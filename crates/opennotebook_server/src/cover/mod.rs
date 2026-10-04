//! Collection covers: designed by a model as a small spec, drawn here.
//!
//! The model never writes markup. It reads what a collection holds (the
//! [`crate::collection::Digest`] naming reads too) and answers with a topic,
//! a few terms, and one choice from each of three short lists: a motif glyph,
//! a palette and a layout. [`validate`] holds the answer to those lists and
//! [`render`] draws it. Until a collection has a designed cover, with covers
//! off, or when the model fails, the cover is [`fallback`]: the same drawing
//! from the cid and title, which costs nothing.
//!
//! When a cover is designed and how the row is written is
//! `collection::redraw`, beside the naming it follows.

pub(crate) mod motifs;
pub(crate) mod render;

use std::time::Duration;

use opennotebook_session::CoverSpec;

use crate::collection::Digest;

/// Bumped whenever the drawing changes, so every cached cover is redrawn.
const RENDER_VERSION: u32 = 2;

/// How long a design may take before the collection keeps the cover it has.
pub(crate) const DESIGN_TIMEOUT: Duration = Duration::from_secs(30);

const TOPIC_MAX_CHARS: usize = 60;
const TOPIC_MAX_WORDS: usize = 8;
const TERM_MAX_CHARS: usize = 32;
const TERM_MAX_WORDS: usize = 4;
const TERMS_MAX: usize = 5;

/// FNV-1a: stable across builds and machines, which a cover version and a
/// cid's shapes must be. Not a security hash.
pub(crate) fn hash(parts: &[&str]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for p in parts {
        for b in p.bytes().chain(std::iter::once(0)) {
            h = (h ^ b as u64).wrapping_mul(0x0100_0000_01b3);
        }
    }
    h
}

/// What decides a cover's shapes, palette and layout when nothing else does.
fn seed(cid: &str) -> u64 {
    // FNV's low bits barely move between cids a millisecond apart; a
    // finaliser spreads them over every palette and layout.
    let mut z = hash(&["cover", cid]);
    z = (z ^ (z >> 33)).wrapping_mul(0xff51_afd7_ed55_8ccd);
    z = (z ^ (z >> 33)).wrapping_mul(0xc4ce_b9fe_1a85_ec53);
    z ^ (z >> 33)
}

/// The version of a designed cover: its spec, drawn by this renderer.
pub(crate) fn version(spec: &CoverSpec) -> String {
    let rv = RENDER_VERSION.to_string();
    let terms = spec.terms.join("\u{1f}");
    format!(
        "g-{:016x}",
        hash(&[
            &rv,
            &spec.topic,
            &terms,
            &spec.motif,
            &spec.palette,
            &spec.layout
        ])
    )
}

/// The version of the cover drawn from the cid and title: everything
/// [`fallback`] reads, the source names standing in for their titles.
pub(crate) fn fallback_version(cid: &str, title: &str, source_names: &[String]) -> String {
    let rv = RENDER_VERSION.to_string();
    let names = source_names
        .iter()
        .take(TERMS_MAX)
        .map(String::as_str)
        .collect::<Vec<_>>()
        .join("\u{1f}");
    format!("f-{:016x}", hash(&[&rv, cid, title.trim(), &names]))
}

/// The cover drawn from the cid and title, free and always there: accent and
/// layout from the cid, the title as the topic, the first sources' titles as
/// the terms.
pub(crate) fn fallback(cid: &str, title: &str, source_titles: &[String]) -> CoverSpec {
    let s = seed(cid);
    let topic = clean_topic(title).unwrap_or_else(|| "Untitled collection".to_string());
    CoverSpec {
        topic,
        terms: clean_terms(source_titles.iter().map(String::as_str)),
        motif: motifs::FALLBACK.to_string(),
        palette: render::ACCENTS[(s % render::ACCENTS.len() as u64) as usize]
            .id
            .to_string(),
        layout: render::LAYOUTS[((s >> 8) % render::LAYOUTS.len() as u64) as usize]
            .0
            .to_string(),
    }
}

/// The page for a cover in the viewer's theme. A stored spec is held to the
/// lists again, so one written by an older build, with a motif since dropped
/// or one of the first palettes, still draws.
pub(crate) fn render(cid: &str, spec: &CoverSpec, theme: render::Theme) -> String {
    let spec = repaired(cid, spec.clone());
    let accent = render::accent(&spec.palette).unwrap_or(&render::ACCENTS[0]);
    render::page(&render::Cover {
        topic: &spec.topic,
        terms: &spec.terms,
        motif: &spec.motif,
        accent,
        layout: &spec.layout,
        theme,
        seed: seed(cid),
    })
}

// ── the model's answer ──────────────────────────────────────────────────────

/// The model's reply as a cover, or why it is not one.
///
/// Strict where an answer cannot be repaired: it must be one JSON object with
/// a topic. A motif, accent or layout not on the lists is replaced, not
/// refused: the topic and terms are the expensive part, and the rest has a
/// sound default.
pub(crate) fn validate(cid: &str, raw: &str) -> Result<CoverSpec, String> {
    #[derive(serde::Deserialize)]
    struct Reply {
        topic: String,
        #[serde(default)]
        terms: Vec<serde_json::Value>,
        #[serde(default)]
        motif: String,
        #[serde(default)]
        palette: String,
        #[serde(default)]
        layout: String,
    }
    let json = object_in(raw).ok_or("the reply holds no JSON object")?;
    let r: Reply = serde_json::from_str(json).map_err(|e| format!("not a cover: {e}"))?;
    let topic = clean_topic(&r.topic).ok_or("the topic is empty or a sentence")?;
    let terms = clean_terms(r.terms.iter().filter_map(|t| t.as_str()));
    Ok(repaired(
        cid,
        CoverSpec {
            topic,
            terms,
            motif: r.motif.trim().to_lowercase(),
            palette: r.palette.trim().to_lowercase(),
            layout: r.layout.trim().to_lowercase(),
        },
    ))
}

/// Each choice held to its list: an unknown motif is the collection glyph, an
/// unknown accent or layout the cid's own. One of the first palettes or
/// layouts is read as its nearest accent or layout.
fn repaired(cid: &str, mut spec: CoverSpec) -> CoverSpec {
    let fb = || fallback(cid, "", &[]);
    spec.motif = motifs::resolve(&spec.motif)
        .unwrap_or(motifs::FALLBACK)
        .to_string();
    spec.palette = match render::accent(&spec.palette) {
        Some(a) => a.id.to_string(),
        None => fb().palette,
    };
    spec.layout = match render::layout(&spec.layout) {
        Some(l) => l.to_string(),
        None => fb().layout,
    };
    spec
}

/// The first `{` to the last `}`: past a code fence or a sentence before it.
fn object_in(raw: &str) -> Option<&str> {
    let (a, b) = (raw.find('{')?, raw.rfind('}')?);
    (a < b).then(|| &raw[a..=b])
}

fn one_line(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn trimmed(s: &str) -> String {
    one_line(s)
        .trim_matches(['"', '\'', '*', '“', '”', '‘', '’', '`', '#'])
        .trim_end_matches(['.', ';', ':', ','])
        .trim()
        .to_string()
}

/// A topic as a cover prints it: one line of a few words. A long title is
/// clipped on a word; None when nothing is left.
fn clean_topic(raw: &str) -> Option<String> {
    let t = trimmed(raw);
    if t.is_empty() {
        return None;
    }
    let words: Vec<&str> = t.split_whitespace().collect();
    let t = if words.len() > TOPIC_MAX_WORDS {
        words[..TOPIC_MAX_WORDS].join(" ")
    } else {
        t
    };
    Some(clip_chars(&t, TOPIC_MAX_CHARS))
}

/// Up to [`TERMS_MAX`] distinct terms, each a few words.
fn clean_terms<'a>(raw: impl Iterator<Item = &'a str>) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for t in raw {
        let t = trimmed(t);
        let words: Vec<&str> = t.split_whitespace().take(TERM_MAX_WORDS).collect();
        let t = clip_chars(&words.join(" "), TERM_MAX_CHARS);
        if !t.is_empty() && !out.iter().any(|o| o.eq_ignore_ascii_case(&t)) {
            out.push(t);
        }
        if out.len() == TERMS_MAX {
            break;
        }
    }
    out
}

/// At most `max` characters, cut on a word when there is one to cut on.
fn clip_chars(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_string();
    }
    let cut: String = s.chars().take(max).collect();
    match cut.rfind(' ') {
        Some(i) if i > max / 2 => cut[..i].trim_end().to_string(),
        _ => cut,
    }
}

// ── the model call ──────────────────────────────────────────────────────────

/// The system prompt: the three lists and what each choice suits.
pub(crate) fn system_prompt(language: &str) -> String {
    let motifs = motifs::names().collect::<Vec<_>>().join(", ");
    let palettes = render::ACCENTS
        .iter()
        .map(|a| format!("- {}: {}", a.id, a.mood))
        .collect::<Vec<_>>()
        .join("\n");
    let layouts = render::LAYOUTS
        .iter()
        .map(|(id, what)| format!("- {id}: {what}"))
        .collect::<Vec<_>>()
        .join("\n");
    format!(
        "You design the cover of a collection of study material from what it contains. \
         Reply with one JSON object and nothing else, no code fence:\n\
         {{\"topic\": \"...\", \"terms\": [\"...\"], \"motif\": \"...\", \"palette\": \"...\", \"layout\": \"...\"}}\n\n\
         topic: the subject as a cover would print it, 2 to 5 words, title case, no quotes. \
         Not the collection title when that is long.\n\
         terms: 3 to 5 key terms from the content, 1 to 3 words each.\n\
         motif: the one symbol that best pictures the subject, exactly one of: {motifs}. \
         Pick the closest picture of the subject itself: plants and photosynthesis are tree or flower1, \
         space is rocket or stars, speech models are mic, visas and jobs are briefcase, history is bank. \
         collection is a last resort, only when nothing on the list is even close.\n\
         palette: the cover's one colour, exactly one of:\n{palettes}\n\
         layout: exactly one of:\n{layouts}\n\
         {language}"
    )
}

/// One model call: the digest in, a validated cover out. The spend is logged
/// like every studio call outside a prep.
pub(crate) async fn design(cid: &str, digest: &Digest) -> Result<CoverSpec, String> {
    use opennotebook_ai::Message;

    let provider = opennotebook_session::ai::provider().await?;
    let model = opennotebook_session::settings::agent_model().await;
    let language = opennotebook_session::settings::language_rule(
        &opennotebook_session::settings::language().await,
    );
    let resp = provider
        .completions()
        .model(&model)
        .message(Message::system(system_prompt(&language)))
        .user(digest.cover_prompt())
        .max_tokens(300)
        .send()
        .await
        .map_err(|e| format!("the model could not design it: {e}"))?;
    opennotebook_session::spend::record("cover", &model, resp.usage.as_ref());
    validate(cid, &resp.text)
}

#[cfg(test)]
mod tests {
    use super::*;

    const CID: &str = "s1750000000000";

    #[test]
    fn a_good_reply_is_taken_as_it_is() {
        let s = validate(
            CID,
            r#"{"topic":"Cell Biology","terms":["Mitosis","Organelles","DNA Replication"],"motif":"virus","palette":"green","layout":"map"}"#,
        )
        .unwrap();
        assert_eq!(s.topic, "Cell Biology");
        assert_eq!(s.terms, ["Mitosis", "Organelles", "DNA Replication"]);
        assert_eq!(
            (s.motif.as_str(), s.palette.as_str(), s.layout.as_str()),
            ("virus", "green", "map")
        );
    }

    #[test]
    fn a_fenced_reply_with_a_preamble_is_still_read() {
        let raw = "Here is the cover:\n```json\n{\"topic\": \"\\\"Rust Async.\\\"\", \"terms\": [\"Futures\", \"futures\", 7, \"Tokio\"], \"motif\": \"CPU\", \"palette\": \"Graphite\", \"layout\": \"editorial\"}\n```";
        let s = validate(CID, raw).unwrap();
        assert_eq!(s.topic, "Rust Async");
        assert_eq!(
            s.terms,
            ["Futures", "Tokio"],
            "duplicates and non-strings dropped"
        );
        assert_eq!((s.motif.as_str(), s.palette.as_str()), ("cpu", "slate"));
        let old = validate(
            CID,
            r#"{"topic":"Rome","motif":"bank","palette":"sand","layout":"emblem"}"#,
        )
        .unwrap();
        assert_eq!(
            (old.palette.as_str(), old.layout.as_str()),
            ("amber", "card"),
            "the first palettes and layouts are read as their nearest"
        );
    }

    #[test]
    fn bad_json_or_no_topic_is_refused() {
        assert!(validate(CID, "I cannot do that").is_err());
        assert!(validate(CID, "{\"topic\": ").is_err());
        assert!(validate(CID, "{\"terms\": [\"a\"]}").is_err());
        assert!(validate(CID, "{\"topic\": \"  \"}").is_err());
        assert!(validate(CID, "[\"topic\"]").is_err());
    }

    #[test]
    fn unknown_choices_fall_back_one_by_one() {
        let s = validate(
            CID,
            r#"{"topic":"Volcanoes","terms":["Magma"],"motif":"volcano","palette":"neon","layout":"collage"}"#,
        )
        .unwrap();
        let fb = fallback(CID, "", &[]);
        assert_eq!(s.motif, motifs::FALLBACK);
        assert_eq!(s.palette, fb.palette);
        assert_eq!(s.layout, fb.layout);
        assert_eq!(s.topic, "Volcanoes", "the words are kept");
    }

    #[test]
    fn long_topics_and_terms_are_clipped() {
        let raw = format!(
            r#"{{"topic":"{}","terms":["{}","a b c d e f","x","y","z","w","v"]}}"#,
            "Very ".repeat(40),
            "Supercalifragilistic".repeat(4)
        );
        let s = validate(CID, &raw).unwrap();
        assert!(s.topic.chars().count() <= TOPIC_MAX_CHARS, "{}", s.topic);
        assert!(s.topic.split_whitespace().count() <= TOPIC_MAX_WORDS);
        assert_eq!(s.terms.len(), TERMS_MAX);
        assert!(
            s.terms.iter().all(|t| t.chars().count() <= TERM_MAX_CHARS),
            "{:?}",
            s.terms
        );
        assert_eq!(s.terms[1], "a b c d");
    }

    #[test]
    fn the_fallback_is_the_cids_own_and_never_untitled_blank() {
        let a = fallback("s1", "", &[]);
        assert_eq!(a.topic, "Untitled collection");
        assert_eq!(a, fallback("s1", "", &[]), "deterministic");
        let titles = ["Intro to Kernels".to_string(), "Scheduling".to_string()];
        let b = fallback("s1", "Linux Internals", &titles);
        assert_eq!(b.topic, "Linux Internals");
        assert_eq!(b.terms, titles);
        assert_eq!(
            (a.palette.as_str(), a.layout.as_str()),
            (b.palette.as_str(), b.layout.as_str())
        );
        // Across many cids every accent and layout comes up.
        let mut pals = std::collections::BTreeSet::new();
        let mut lays = std::collections::BTreeSet::new();
        for i in 0..400 {
            let f = fallback(&format!("s17{i:011}"), "", &[]);
            pals.insert(f.palette);
            lays.insert(f.layout);
        }
        assert_eq!(pals.len(), render::ACCENTS.len());
        assert_eq!(lays.len(), render::LAYOUTS.len());
    }

    #[test]
    fn versions_change_with_what_is_drawn_and_only_then() {
        let s = fallback(CID, "Topic", &[]);
        let mut t = s.clone();
        assert_eq!(version(&s), version(&t));
        t.palette = if s.palette == "rose" { "teal" } else { "rose" }.into();
        assert_ne!(version(&s), version(&t));
        assert!(version(&s).starts_with("g-"));
        let names = vec!["a.md".to_string()];
        let f = fallback_version(CID, "Topic", &names);
        assert!(f.starts_with("f-"));
        assert_eq!(f, fallback_version(CID, " Topic ", &names));
        assert_ne!(f, fallback_version(CID, "Other", &names));
        assert_ne!(f, fallback_version(CID, "Topic", &[]));
    }

    fn assert_safe(html: &str) {
        assert!(html.starts_with("<!doctype html>"));
        let lower = html.to_lowercase();
        assert!(!lower.contains("<script"), "no script");
        let rest = lower.replace("http://www.w3.org/2000/svg", "");
        assert!(
            !rest.contains("http:") && !rest.contains("https:") && !rest.contains("//"),
            "nothing fetched"
        );
        assert!(
            !lower.contains("url(http") && !lower.contains("@import") && !lower.contains("<img")
        );
        assert!(lower.contains("content-security-policy"));
        assert!(html.contains(r#"viewBox="0 0 1600 900""#));
        assert!(html.contains(r#"preserveAspectRatio="xMidYMid slice""#));
    }

    fn attr(el: &str, k: &str) -> Option<f64> {
        let at = el.find(&format!(" {k}=\""))? + k.len() + 3;
        el[at..at + el[at..].find('"')?].parse().ok()
    }

    /// Every `<text>`, every chip and every tile stays inside the margin a
    /// card's padding leaves, by the renderer's own measure, and no text
    /// leaves the box it was fitted to.
    fn assert_on_canvas(html: &str) {
        for t in html.split("<text ").skip(1) {
            let (x, y, size) = (
                attr(t, "x").unwrap(),
                attr(t, "y").unwrap(),
                attr(t, "font-size").unwrap(),
            );
            assert!(y > size * 0.7 && y < render::H, "y {y} off the canvas: {t}");
            assert!((0.0..=render::W).contains(&x), "x {x}: {t}");
        }
        for r in html.split("<rect ").skip(1) {
            let r = &r[..r.find('>').unwrap()];
            let (Some(x), Some(y), Some(w), Some(h)) = (
                attr(r, "x"),
                attr(r, "y"),
                attr(r, "width"),
                attr(r, "height"),
            ) else {
                continue;
            };
            assert!(
                x >= 40.0 && y >= 40.0 && x + w <= render::W - 40.0 && y + h <= render::H - 40.0,
                "a shape past the margin: {r}"
            );
        }
    }

    /// The topic's size: at least 80, a 9 px cap height on a 256 px card.
    fn assert_topic_legible(html: &str) {
        let sizes: Vec<f64> = html
            .split(r#"<text class="t" "#)
            .skip(1)
            .map(|t| attr(&format!(" {t}"), "font-size").unwrap())
            .collect();
        assert!(!sizes.is_empty(), "a topic is drawn");
        for s in sizes {
            assert!(s * 0.72 * 256.0 / render::W >= 9.0, "topic at {s}");
        }
    }

    #[test]
    fn every_layout_and_accent_draws_safely_with_hostile_text() {
        let hostile: Vec<String> = vec![
            "<script>alert(1)</script>".into(),
            "Ünïcødé & \"quotes\"".into(),
            "Photosynthesis".into(),
            "量子力学の基礎".into(),
            "x".repeat(32),
        ];
        let long = "The Extraordinarily Long History of Pneumonoultramicroscopic Things";
        let cases: [(&str, &[String]); 4] = [
            (long, &hostile),
            ("Cells", &hostile[2..3]),
            ("Cells", &[]),
            (&"W".repeat(60), &hostile[..2]),
        ];
        for (i, a) in render::ACCENTS.iter().enumerate() {
            for (l, _) in render::LAYOUTS {
                for (topic, terms) in cases {
                    for theme in [render::Theme::Dark, render::Theme::Light] {
                        let spec = CoverSpec {
                            topic: topic.into(),
                            terms: terms.to_vec(),
                            motif: "globe-americas".into(),
                            palette: a.id.into(),
                            layout: l.to_string(),
                        };
                        let html = render(&format!("s17{i:011}"), &spec, theme);
                        assert_safe(&html);
                        assert_on_canvas(&html);
                        assert_topic_legible(&html);
                        assert!(!html.contains("<script>alert"), "escaped");
                        assert!(html.contains(a.fill), "{l}/{}", a.id);
                    }
                }
            }
        }
    }

    #[test]
    fn a_stored_spec_with_unknown_choices_still_draws() {
        let spec = CoverSpec {
            topic: "T".into(),
            terms: vec![],
            motif: "gone".into(),
            palette: "gone".into(),
            layout: "gone".into(),
        };
        let html = render(CID, &spec, render::Theme::Dark);
        assert_safe(&html);
        assert!(html.contains("<path"), "the fallback glyph is drawn");
    }

    /// Writes every layout in every accent, and a sheet of them on cards the
    /// size the home page draws them, in each theme, for a person to look at:
    /// `COVER_SAMPLES=<dir> cargo test -p opennotebook_server cover_samples -- --ignored`.
    #[test]
    #[ignore]
    fn cover_samples() {
        let dir = std::path::PathBuf::from(std::env::var("COVER_SAMPLES").expect("COVER_SAMPLES"));
        std::fs::create_dir_all(&dir).unwrap();
        let subjects: &[(&str, &[&str], &str)] = &[
            (
                "Cell Biology",
                &["Mitosis", "Organelles", "DNA Replication", "Membranes"],
                "virus",
            ),
            (
                "The Roman Republic",
                &[
                    "Senate",
                    "Consuls",
                    "Punic Wars",
                    "Julius Caesar",
                    "Res Publica",
                ],
                "bank",
            ),
            ("Async Rust", &["Futures", "Tokio", "Pinning"], "cpu"),
            (
                "Climate and Oceans",
                &["Currents", "Carbon Cycle", "Sea Level", "El Niño"],
                "water",
            ),
            (
                "Jazz Harmony",
                &["ii–V–I", "Voicings", "Modes", "Tritone Substitution"],
                "music-note-beamed",
            ),
            (
                "Behavioural Economics and the Psychology of Everyday Choices",
                &["Nudges", "Loss Aversion", "Anchoring"],
                "graph-up",
            ),
            (
                "Untitled collection",
                &[
                    "Moshi: a speech-text foundation model",
                    "Quantifying Variance",
                ],
                "collection",
            ),
            (
                "Exploring the Solar System",
                &["Planets", "Orbits", "Kepler's Laws", "Moons"],
                "moon-stars",
            ),
            ("Plant Life", &[], "flower1"),
            (
                "Machine Learning Basics",
                &[
                    "Gradient Descent",
                    "Overfitting",
                    "Loss Functions",
                    "Neural Networks",
                    "Regularisation",
                ],
                "diagram-3",
            ),
        ];
        for (theme, name) in [
            (render::Theme::Dark, "dark"),
            (render::Theme::Light, "light"),
        ] {
            let attr = if name == "light" {
                r#" data-bs-theme="light""#
            } else {
                ""
            };
            // The app's card around each cover: 272 wide with 8 of padding,
            // as on the home page at 1400 px.
            let mut sheet = format!(
                "<!doctype html><html{attr}><meta charset=utf-8><style>{}\
                 body{{margin:0;padding:24px;background:var(--bs-body-bg);color:var(--bs-body-color);font:14px var(--bs-font-sans-serif)}}\
                 .g{{display:grid;grid-template-columns:repeat(4,272px);gap:20px}}\
                 .card{{padding:8px;background:var(--bs-secondary-bg);border:1px solid var(--bs-border-color);border-radius:10px}}\
                 .c{{width:254px;aspect-ratio:16/9;overflow:hidden;border-radius:7px;position:relative;box-shadow:inset 0 0 0 1px var(--bs-border-color-translucent)}}\
                 .c iframe{{border:0;width:1600px;height:900px;transform:scale(calc(254/1600));transform-origin:0 0;position:absolute}}\
                 .t{{font-weight:600;color:var(--bs-emphasis-color);margin:10px 4px 2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}\
                 .s{{font-size:12px;color:var(--bs-secondary-color);margin:0 4px 2px}}</style><div class=g>",
                opennotebook_sdk::theme::CSS
            );
            let mut n = 0;
            for (i, (topic, terms, motif)) in subjects.iter().enumerate() {
                for (j, (layout, _)) in render::LAYOUTS.iter().enumerate() {
                    let accent = render::ACCENTS[(i * 3 + j) % render::ACCENTS.len()].id;
                    let spec = CoverSpec {
                        topic: topic.to_string(),
                        terms: terms.iter().map(|t| t.to_string()).collect(),
                        motif: motif.to_string(),
                        palette: accent.into(),
                        layout: layout.to_string(),
                    };
                    let file = format!("{n:02}-{layout}-{accent}-{name}.html");
                    std::fs::write(
                        dir.join(&file),
                        render(&format!("s17{n:011}"), &spec, theme),
                    )
                    .unwrap();
                    sheet.push_str(&format!(
                        "<div class=card><div class=c><iframe src=\"{file}\"></iframe></div><div class=t>{topic}</div><div class=s>{file}</div></div>"
                    ));
                    n += 1;
                }
            }
            sheet.push_str("</div>");
            std::fs::write(dir.join(format!("sheet-{name}.html")), sheet).unwrap();
        }
    }

    /// One real design call through the AI endpoint, printing the reply and
    /// what it cost: `cargo test -p opennotebook_server cover_live -- --ignored --nocapture`.
    #[tokio::test]
    #[ignore]
    async fn cover_live() {
        use crate::collection::{Opening, Part};
        let digest = Digest {
            title: "Linux Kernel Scheduling".into(),
            sources: vec![
                Opening {
                    title: "CFS Scheduler Design".into(),
                    text: "The Completely Fair Scheduler models an ideal, precise multi-tasking CPU on real hardware. It keeps runnable tasks in a red-black tree ordered by virtual runtime, and always picks the task that has had the least time on the CPU. ".repeat(2),
                },
                Opening {
                    title: "EEVDF: Earliest Eligible Virtual Deadline First".into(),
                    text: "EEVDF replaced CFS in Linux 6.6. Each task gets a lag and a virtual deadline; latency-sensitive tasks can ask for shorter slices without taking more CPU overall. ".repeat(2),
                },
            ],
            made: vec![Part {
                kind: "Narrated slides",
                title: "How Linux Picks the Next Task".into(),
                parts: vec!["Why scheduling matters".into(), "Virtual runtime".into(), "The red-black tree".into(), "From CFS to EEVDF".into()],
            }],
        };
        let prompt = format!("{}\n---\n{}", system_prompt(""), digest.cover_prompt());
        eprintln!(
            "prompt: {} chars (~{} tokens)",
            prompt.chars().count(),
            prompt.chars().count() / 4
        );
        let spec = design(CID, &digest).await.unwrap();
        eprintln!("{spec:?}");
    }

    #[test]
    fn the_prompt_names_every_choice() {
        let p = system_prompt("");
        for (m, _) in motifs::MOTIFS {
            assert!(p.contains(m), "{m}");
        }
        for a in render::ACCENTS {
            assert!(p.contains(&format!("- {}:", a.id)));
        }
        for (l, _) in render::LAYOUTS {
            assert!(p.contains(&format!("- {l}:")));
        }
    }
}
