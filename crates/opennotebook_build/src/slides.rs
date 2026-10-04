//! The deck: one HTML document per slide, written by the slide model and drawn
//! with the style's kit ([`crate::kits`]).
//!
//! This replaced an earlier slide service. That service designed each slide with a model of
//! its own, an image model per picture and a visual check, and forbade inline
//! SVG; studio sent it the slide copy and waited, often past its 90-second
//! window, for decks whose colours came from prose it interpreted. Side by side
//! in Slide Lab (the comparison page), one call that writes the slides as HTML
//! with the figures drawn in SVG, and a kit that supplies every colour, font and
//! texture, came out more on-style, faster and cheaper.
//!
//! The content is not the model's to choose: each slide's title and copy come
//! from the script, so the slide shows what the narration says. The model lays
//! that copy out with the kit's classes and draws one figure that explains it.
//!
//! Slides are written a few per call, calls in parallel. A batch that comes back
//! short or broken is asked once more; a slide still missing after that gets a
//! plain slide built here from its copy, so a session never fails over a figure.

use std::path::{Path, PathBuf};

use futures_util::future::join_all;
use opennotebook_session::{SessionSlide, SlideRef};

use crate::error::BuildError;
use crate::kits::{self, Kit};

/// Slides per model call. Four keeps a reply near 10k characters, well inside
/// the output limit, and lets a 12-slide deck go out as three calls at once.
pub const PER_CALL: usize = 4;

/// Output ceiling per call. Measured on Haiku 4.5 (prep jobs 00ks to 00m1,
/// October 2026): 9k to 13k characters for five slides, so about 2k a slide
/// and 8k for a full batch of four. This leaves room for a style that draws
/// far more.
const MAX_TOKENS: u32 = 16_000;

/// The presentation name every studio-written deck lives under.
pub const PRESENTATION: &str = "studio";

/// What the deck is drawn in and where it is written.
pub struct DeckPlan<'a> {
    pub title: &'a str,
    pub style: &'static Kit,
    pub style_label: &'a str,
    pub style_brief: &'a str,
    /// The slide model, e.g. `anthropic/claude-haiku-4.5`.
    pub model: &'a str,
    /// A sentence fixing the output language, or empty for English.
    pub language_rule: &'a str,
    /// The session's deck directory; slides go in `<deck_dir>/studio/`.
    pub deck_dir: &'a Path,
    /// The session sid, which is the deck's collection.
    pub collection: &'a str,
}

/// How the deck came out.
pub struct DeckOutcome {
    /// One per slide, in the order given.
    pub refs: Vec<SlideRef>,
    /// Slides the model did not deliver, drawn as plain slides instead.
    pub fallbacks: usize,
    /// Characters the model was sent and wrote, for the cost log.
    pub chars_in: usize,
    pub chars_out: usize,
}

/// The file a slide of this deck is written to.
pub fn slide_path(deck_dir: &Path, slide: &str) -> PathBuf {
    deck_dir.join(PRESENTATION).join(format!("{slide}.html"))
}

/// Write every slide of the session, in parallel batches.
///
/// The refs come back in slide order (by ordinal); the caller matches them to
/// its slides by name.
pub async fn write_deck(
    plan: &DeckPlan<'_>,
    slides: &[SessionSlide],
) -> Result<DeckOutcome, BuildError> {
    let dir = plan.deck_dir.join(PRESENTATION);
    std::fs::create_dir_all(&dir).map_err(|source| BuildError::SlideWrite {
        path: dir.to_string_lossy().into_owned(),
        source,
    })?;
    let mut order: Vec<&SessionSlide> = slides.iter().collect();
    order.sort_by_key(|s| s.ordinal);
    let total = order.len();

    let batches: Vec<&[&SessionSlide]> = order.chunks(PER_CALL).collect();
    let written = join_all(
        batches
            .iter()
            .enumerate()
            .map(|(i, chunk)| write_batch(plan, chunk, i * PER_CALL, total)),
    )
    .await;

    let mut outcome = DeckOutcome {
        refs: Vec::with_capacity(total),
        fallbacks: 0,
        chars_in: 0,
        chars_out: 0,
    };
    for (chunk, batch) in batches.iter().zip(written) {
        outcome.chars_in += batch.chars_in;
        outcome.chars_out += batch.chars_out;
        for (i, slide) in chunk.iter().enumerate() {
            let html = batch.docs.get(i).cloned().flatten().unwrap_or_else(|| {
                outcome.fallbacks += 1;
                plain_slide(plan, slide)
            });
            let name = &slide.slide_ref.slide;
            let path = slide_path(plan.deck_dir, name);
            std::fs::write(&path, kits::apply(&html, plan.style)).map_err(|source| {
                BuildError::SlideWrite {
                    path: path.to_string_lossy().into_owned(),
                    source,
                }
            })?;
            outcome.refs.push(SlideRef {
                collection: plan.collection.to_string(),
                presentation: PRESENTATION.to_string(),
                slide: name.clone(),
            });
        }
    }
    Ok(outcome)
}

/// What one batch call produced.
struct Batch {
    /// One per slide asked for, in order; `None` where the model never
    /// delivered a usable one.
    docs: Vec<Option<String>>,
    chars_in: usize,
    chars_out: usize,
}

/// One batch: asked once, and once more if any slide came back missing or
/// unusable. A slide that was usable the first time keeps that version.
async fn write_batch(
    plan: &DeckPlan<'_>,
    chunk: &[&SessionSlide],
    start: usize,
    total: usize,
) -> Batch {
    let (system, user) = prompt(plan, chunk, start, total);
    let range = format!("{}–{}", start + 1, start + chunk.len());
    let mut batch = Batch {
        docs: vec![None; chunk.len()],
        chars_in: 0,
        chars_out: 0,
    };
    for attempt in 1..=2 {
        batch.chars_in += system.len() + user.len();
        match complete(plan.model, &system, &user).await {
            Ok(text) => {
                batch.chars_out += text.len();
                for (i, doc) in complete_docs(&text)
                    .into_iter()
                    .take(chunk.len())
                    .enumerate()
                {
                    if batch.docs[i].is_some() {
                        continue;
                    }
                    if usable(&doc) {
                        batch.docs[i] = Some(doc);
                    } else {
                        eprintln!(
                            "opennotebook prep: slide {} came back without its title (attempt {attempt})",
                            start + i + 1
                        );
                    }
                }
                let have = batch.docs.iter().flatten().count();
                if have == chunk.len() {
                    return batch;
                }
                eprintln!(
                    "opennotebook prep: slides {range} came back with {have} of {} usable slides (attempt {attempt})",
                    chunk.len()
                );
            }
            Err(e) => eprintln!(
                "opennotebook prep: slides {range} failed on {} ({e}) (attempt {attempt})",
                plan.model
            ),
        }
    }
    batch
}

async fn complete(model: &str, system: &str, user: &str) -> Result<String, String> {
    let provider = opennotebook_session::ai::provider().await?;
    let response = provider
        .completions()
        .model(model)
        .system(system.to_string())
        .user(user.to_string())
        .max_tokens(MAX_TOKENS)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    opennotebook_session::spend::record("slides", model, response.usage.as_ref());
    Ok(response.text)
}

/// Every complete `<!doctype …>` … `</html>` document in a reply, in order.
/// A slide worth keeping. The cleaner takes care of colours and positioning,
/// so what is left to judge is whether the slide still says what it is about:
/// it has its title, as the kit's title.
pub fn usable(doc: &str) -> bool {
    let doc = crate::clean::clean(doc);
    let lower = doc.to_ascii_lowercase();
    lower.contains("k-title") && lower.contains("<body")
}

pub fn complete_docs(text: &str) -> Vec<String> {
    let lower = text.to_ascii_lowercase();
    let mut out = Vec::new();
    let mut from = 0;
    while let Some(rel) = lower[from..].find("<!doctype") {
        let s = from + rel;
        let Some(erel) = lower[s..].find("</html>") else {
            break;
        };
        let e = s + erel + "</html>".len();
        out.push(text[s..e].to_string());
        from = e;
    }
    out
}

/// The system and user prompts for one batch.
fn prompt(
    plan: &DeckPlan<'_>,
    chunk: &[&SessionSlide],
    start: usize,
    total: usize,
) -> (String, String) {
    let k = plan.style;
    let system = format!(
        r#"You design presentation slides as HTML for a narrated explainer session. You are given each slide's exact copy; lay it out and draw its figure.

STYLE: {label}. {brief}

THE STYLE KIT — this deck has a fixed style kit that is added to every slide after you write it. It sets the fonts, every colour, the background and texture, spacing, and the look of every component and illustration class below. So:
- Write NO CSS for colours, fonts, font sizes, backgrounds, borders, shadows, radii or filters, and no <link> or @import. If a slide needs a layout tweak, a short <style> with only grid/flex/width/height/gap/margin/order is allowed.
- Use only these classes. The <body> is already a column with an 80px margin and a 34px gap; put blocks directly in it.
  k-kicker: small label above a title.   k-title (on <h1>): the slide title.   k-sub: one-line subtitle.
  k-split: two columns, text left and figure right (add k-fig-wide for a larger figure). k-row: a row of 2–4 equal cards. k-stack: a vertical group. k-center: centred block.
  k-points (on <ul>): up to 4 short points.   k-card: a card; an <h3> inside is its heading.   k-stat (with k-card): a stat, holding <div class="k-num"> and <div class="k-cap">.   k-hl (on <span>): highlight one key phrase.
  k-figure (on <figure>): holds the slide's one inline <svg viewBox="…">; a <figcaption class="k-figcap"> inside it is a caption.
  k-bento: a grid of tiles instead of k-split; inside it k-tile elements (k-tile-wide spans two columns, k-tile-tall two rows), each with an <h3> and a <p>, or a <div class="k-num">, or a small inline <svg>.
  k-steps: a row of numbered steps instead of k-split; inside it 3–5 k-step elements, each with <div class="k-n">1</div>, an optional small inline <svg>, an <h3> and a <p>. The kit draws the arrows.
  Inside an SVG use only these classes, never fill/stroke/style attributes of your own: k-ink (a drawn line), k-f0 (paper/white), k-f1 (accent), k-f2 / k-f3 / k-f4 (the style's illustration colours), k-shade (shadow or depth), k-lab (on <text>: a short label, at most 3 words).

ILLUSTRATION RECIPE FOR THIS STYLE (follow it exactly; the kit's filters do the texture):
{recipe}
A small example of the technique (draw your own subject, larger and more detailed, 10–25 elements):
{example}

SLIDE STRUCTURE FOR THIS STYLE (its signature layout — use it):
{structure}

LAYOUT: {layout_rule} Vary the composition from slide to slide.

RULES:
1. Each slide is ONE complete HTML document from <!doctype html> to </html>. No <script>, no images, no emoji, no url() except url(#k-…) inside an SVG.
2. Nothing may extend past 1920×1080: keep to at most 4 points or 3 cards or 7 tiles, short lines, and one figure.
3. Text never sits on an SVG. Labels inside an SVG use k-lab: at most 12 characters, centred with text-anchor="middle", inside the viewBox with room to spare, at most 6 per figure.
4. Use the given copy: the title as the k-title, the points, stats and subheads as given (you may shorten them). A slide needs substance: if it has fewer than three points, add short points taken from its narration excerpt (only what the narration says, never outside facts). The figure explains what the slide is about and is not decoration. Use k-num only for a real number or short code from the copy or narration (a count, a percentage, a PID); never for a word like "Varies" — without one, use an <h3> instead.
5. One flat page on the kit's background: the kicker, the title and the content are direct children of <body>, never wrapped in an extra container. No position:absolute or fixed, no layers or overlays, no background shapes, no full-slide rectangles, no gradients and no opacity: everything is drawn at full strength, and a figure sits in the layout beside or below the text, never behind it.
6. {language}

OUTPUT FORMAT — the slides in the order given, nothing else, no commentary, no Markdown fences:
<!-- SLIDE n -->
<!doctype html>
...
</html>"#,
        label = plan.style_label,
        brief = plan.style_brief,
        recipe = k.recipe,
        example = k.example,
        layout_rule = k.layout_rule,
        structure = kits::structure(k),
        language = if plan.language_rule.is_empty() {
            "Write any extra short labels in English.".to_string()
        } else {
            format!(
                "{} Keep the class names and HTML exactly as specified.",
                plan.language_rule
            )
        },
    );

    let mut user = format!(
        "Session: {}\nThis batch is slides {}–{} of {}.\n",
        plan.title,
        start + 1,
        start + chunk.len(),
        total
    );
    for (i, s) in chunk.iter().enumerate() {
        let n = start + i + 1;
        let said: String = s
            .lines
            .iter()
            .map(|l| l.text.as_str())
            .collect::<Vec<_>>()
            .join(" ");
        let said: String = said.chars().take(500).collect();
        user.push_str(&format!("\n=== SLIDE {n} ===\nTitle: {}\n", s.title));
        if n == 1 {
            user.push_str("This slide opens the session: give it a title-slide treatment (a large title with a k-kicker and a k-sub, and a figure).\n");
        }
        for e in s.on_slide.iter().filter(|e| !e.starts_with("[image:")) {
            user.push_str(e);
            user.push('\n');
        }
        user.push_str(&format!("Narration excerpt: {said}\n"));
    }
    (system, user)
}

/// A slide built here from its copy, for one the model did not deliver: the
/// title, its points, its stat. No figure, but on-style and readable.
pub fn plain_slide(plan: &DeckPlan<'_>, s: &SessionSlide) -> String {
    let esc = |t: &str| {
        t.replace('&', "&amp;")
            .replace('<', "&lt;")
            .replace('>', "&gt;")
    };
    let mut points = String::new();
    let mut stat = String::new();
    let mut sub = String::new();
    for e in &s.on_slide {
        if let Some(p) = e.strip_prefix("Point:") {
            points.push_str(&format!("<li>{}</li>", esc(p.trim())));
        } else if let Some(v) = e.strip_prefix("Stat:") {
            let (num, cap) = v.split_once('—').unwrap_or((v, ""));
            stat = format!(
                "<div class=\"k-card k-stat\"><div class=\"k-num\">{}</div><div class=\"k-cap\">{}</div></div>",
                esc(num.trim()),
                esc(cap.trim())
            );
        } else if let Some(v) = e.strip_prefix("Subhead:") {
            sub = format!("<p class=\"k-sub\">{}</p>", esc(v.trim()));
        }
    }
    let body = if points.is_empty() && stat.is_empty() {
        String::new()
    } else {
        format!("<div class=\"k-stack\"><ul class=\"k-points\">{points}</ul>{stat}</div>")
    };
    format!(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>{t}</title></head><body>\
         <div class=\"k-kicker\">{deck}</div><h1 class=\"k-title\">{t}</h1>{sub}{body}</body></html>",
        t = esc(&s.title),
        deck = esc(plan.title),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn documents_are_cut_out_of_a_reply_in_order() {
        let reply = "<!-- SLIDE 1 -->\n<!doctype html><html><body>a</body></html>\nnote\n\
                     <!-- SLIDE 2 -->\n<!DOCTYPE html><html><body>b</body></html>\n\
                     <!-- SLIDE 3 -->\n<!doctype html><html><body>cut";
        let docs = complete_docs(reply);
        assert_eq!(docs.len(), 2, "the unfinished third is not a slide");
        assert!(docs[0].contains(">a<") && docs[1].contains(">b<"));
    }
}
