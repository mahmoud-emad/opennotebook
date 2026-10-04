//! Drawing a cover: a validated [`Cover`] into one self-contained HTML page.
//!
//! A cover is drawn with the studio's own parts, so it reads as part of the
//! app: the page and its dot grid as on the mind map's canvas, the motif in a
//! rounded tile like a Create tile's glyph, the terms as chips, a small
//! uppercase label, and the title in the app's sans at 600. Every colour but
//! one is a theme token from `theme.css`, embedded in the page and switched by
//! `data-bs-theme` exactly as the app switches it, so a cover follows the
//! viewer's theme. The one colour of its own is the collection's accent, on
//! the motif's tile and the short rule before the label.
//!
//! The page is a 1600x900 SVG with system fonts, no script and nothing
//! fetched, so the UI can frame it in a sandboxed iframe and scale it to a
//! card, about 0.16 to 0.19 of its size. Sizes, radii and hairlines are
//! chosen for that scale: a radius of 56 here is the 10 of an app card. Text
//! is laid out here, not by the browser: an SVG `<text>` never wraps, so every
//! line is measured with a per-glyph width estimate, the size is stepped down
//! until the text fits its box, and what still does not fit is cut with an
//! ellipsis. The estimates lean wide, so a line drawn in a narrower font than
//! guessed only leaves more margin.

use std::fmt::Write as _;

use super::motifs;

pub(crate) const W: f64 = 1600.0;
pub(crate) const H: f64 = 900.0;

/// The theme a cover is drawn in: the app's own, dark unless the viewer chose
/// light.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) enum Theme {
    #[default]
    Dark,
    Light,
}

impl Theme {
    /// `light` is light; anything else is the studio's default.
    pub(crate) fn parse(s: &str) -> Self {
        if s.trim().eq_ignore_ascii_case("light") {
            Theme::Light
        } else {
            Theme::Dark
        }
    }
}

/// A collection's one colour. The same in both themes, as the app's primary
/// is: white reads on it at 4.5:1, and it stands at 3:1 against the page and
/// the card surface of either theme; the tests hold every accent to that.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Accent {
    pub id: &'static str,
    /// What the prompt tells the model it suits.
    pub mood: &'static str,
    pub fill: &'static str,
    /// The motif on the fill.
    pub on_fill: &'static str,
}

pub(crate) const ACCENTS: &[Accent] = &[
    Accent {
        id: "blue",
        mood: "the studio's own blue, for science, technology and anything general",
        fill: "#2563eb",
        on_fill: "#ffffff",
    },
    Accent {
        id: "teal",
        mood: "for the sea, climate, data, medicine",
        fill: "#0f7a72",
        on_fill: "#ffffff",
    },
    Accent {
        id: "green",
        mood: "for nature, biology, ecology, health",
        fill: "#15803d",
        on_fill: "#ffffff",
    },
    Accent {
        id: "amber",
        mood: "for history, economics, finance, craft",
        fill: "#a16207",
        on_fill: "#ffffff",
    },
    Accent {
        id: "coral",
        mood: "for geography, travel, food, culture",
        fill: "#c2410c",
        on_fill: "#ffffff",
    },
    Accent {
        id: "rose",
        mood: "for the arts, music, people, literature",
        fill: "#d42a5c",
        on_fill: "#ffffff",
    },
    Accent {
        id: "violet",
        mood: "for ideas, philosophy, language, space",
        fill: "#7c4dee",
        on_fill: "#ffffff",
    },
    Accent {
        id: "slate",
        mood: "for computing, engineering, mathematics, law",
        fill: "#64748b",
        on_fill: "#ffffff",
    },
];

/// The palettes covers were first designed with, each read as the accent
/// nearest it, so a stored spec or an old answer still draws.
const OLD_PALETTES: &[(&str, &str)] = &[
    ("paper", "coral"),
    ("sage", "green"),
    ("sky", "blue"),
    ("blush", "rose"),
    ("lilac", "violet"),
    ("sand", "amber"),
    ("ocean", "teal"),
    ("dusk", "violet"),
    ("forest", "green"),
    ("graphite", "slate"),
];

/// The three arrangements, with what the prompt says each suits.
pub(crate) const LAYOUTS: &[(&str, &str)] = &[
    (
        "tile",
        "the symbol in its tile above a large topic, for most subjects",
    ),
    (
        "card",
        "the topic on a card with the symbol and the terms, for a subject with a few clear parts",
    ),
    (
        "map",
        "the symbol as the root of a small map of the terms, for a subject of connected ideas",
    ),
];

/// The first layouts, each read as the one nearest it.
const OLD_LAYOUTS: &[(&str, &str)] = &[
    ("emblem", "card"),
    ("editorial", "tile"),
    ("grid", "map"),
    ("horizon", "tile"),
];

/// The accent an id names, an old palette id included.
pub(crate) fn accent(id: &str) -> Option<&'static Accent> {
    let id = OLD_PALETTES
        .iter()
        .find(|(old, _)| *old == id)
        .map_or(id, |(_, new)| new);
    ACCENTS.iter().find(|a| a.id == id)
}

/// The layout an id names, an old layout id included.
pub(crate) fn layout(id: &str) -> Option<&'static str> {
    let id = OLD_LAYOUTS
        .iter()
        .find(|(old, _)| *old == id)
        .map_or(id, |(_, new)| new);
    LAYOUTS.iter().map(|(l, _)| *l).find(|l| *l == id)
}

/// A cover ready to draw: every field already checked against the lists.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Cover<'a> {
    pub topic: &'a str,
    pub terms: &'a [String],
    pub motif: &'a str,
    pub accent: &'a Accent,
    pub layout: &'a str,
    pub theme: Theme,
    /// Places the tile and the grid, so two covers alike still differ.
    pub seed: u64,
}

// ── text ────────────────────────────────────────────────────────────────────

/// The kicker over every topic, as a section label sets it.
const KICKER: &str = "COLLECTION";

#[derive(Debug, Clone, Copy, PartialEq)]
enum Face {
    /// A title: 600, the heaviest the studio sets.
    Title,
    /// A chip's words: 500.
    Chip,
}

impl Face {
    fn class(self) -> &'static str {
        match self {
            Face::Title => "t",
            Face::Chip => "c",
        }
    }
    /// How much wider than the estimate this face sets, at most: measured
    /// against DejaVu Sans at 600 and 500, the widest of the stack.
    fn factor(self) -> f64 {
        match self {
            Face::Title => 1.18,
            Face::Chip => 1.08,
        }
    }
}

/// A glyph's advance in ems for a bold sans, rounded up: wide enough for
/// Segoe UI, Roboto, Helvetica and Inter alike. Wider faces, such as the
/// DejaVu Sans many Linux desktops resolve `system-ui` to, are covered by
/// each [`Face`]'s factor.
fn em(c: char) -> f64 {
    match c {
        ' ' => 0.28,
        'i' | 'l' | 'j' | '\'' | '|' | '!' | '.' | ',' | ':' | ';' | '·' => 0.3,
        'f' | 't' | 'r' | 'I' | '(' | ')' | '[' | ']' | '-' => 0.4,
        'm' | 'w' => 0.9,
        'M' | 'W' | '@' | '%' => 0.98,
        '—' | '…' => 1.0,
        c if c.is_ascii_uppercase() => 0.72,
        c if c.is_ascii_digit() => 0.6,
        c if c.is_ascii() => 0.58,
        // CJK, kana, hangul and other full-width scripts.
        c if ('\u{1100}'..='\u{FFEF}').contains(&c) && !('\u{2000}'..='\u{2E7F}').contains(&c) => {
            1.04
        }
        _ => 0.66,
    }
}

fn width(s: &str, size: f64, face: Face) -> f64 {
    s.chars().map(em).sum::<f64>() * size * face.factor()
}

/// Greedy word wrap; a word longer than a line is broken by characters, so a
/// long compound or a line of CJK still fits.
fn wrap(text: &str, size: f64, max_w: f64, face: Face) -> Vec<String> {
    let mut lines: Vec<String> = Vec::new();
    let mut cur = String::new();
    for word in text.split_whitespace() {
        let joined = if cur.is_empty() {
            word.to_string()
        } else {
            format!("{cur} {word}")
        };
        if width(&joined, size, face) <= max_w {
            cur = joined;
            continue;
        }
        if !cur.is_empty() {
            lines.push(std::mem::take(&mut cur));
        }
        if width(word, size, face) <= max_w {
            cur = word.to_string();
            continue;
        }
        for c in word.chars() {
            let mut next = cur.clone();
            next.push(c);
            if width(&next, size, face) > max_w && !cur.is_empty() {
                lines.push(std::mem::take(&mut cur));
                cur.push(c);
            } else {
                cur = next;
            }
        }
    }
    if !cur.is_empty() {
        lines.push(cur);
    }
    lines
}

/// `s` cut to `max_w`, with an ellipsis when anything was cut.
fn clip_line(s: &str, size: f64, max_w: f64, face: Face) -> String {
    if width(s, size, face) <= max_w {
        return s.to_string();
    }
    let mut out: String = s.to_string();
    while !out.is_empty() && width(&format!("{out}…"), size, face) > max_w {
        out.pop();
    }
    format!("{}…", out.trim_end_matches([' ', ',', ':', ';', '-', '·']))
}

/// Text set in a box: the size it fits at and its lines.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Fitted {
    pub size: f64,
    pub lines: Vec<String>,
}

/// The largest size from `max` down to `min` at which `text` wraps into at
/// most `max_lines` lines of `max_w` within `max_h`; at `min` the lines past
/// the last are dropped and the last one ends in an ellipsis.
fn fit(
    text: &str,
    face: Face,
    max_w: f64,
    max_h: f64,
    max_lines: usize,
    max: f64,
    min: f64,
) -> Fitted {
    let leading = 1.12;
    let mut size = max;
    loop {
        let lines = wrap(text, size, max_w, face);
        let fits_h = lines.len() as f64 * size * leading <= max_h;
        // A word broken across lines reads worse than a smaller size, so
        // breaking one is a last resort taken only at the smallest.
        let whole = text
            .split_whitespace()
            .all(|w| width(w, size, face) <= max_w);
        if lines.len() <= max_lines && fits_h && (whole || size <= min) {
            return Fitted { size, lines };
        }
        if size <= min {
            let room = ((max_h / (size * leading)).floor() as usize).clamp(1, max_lines);
            let mut kept: Vec<String> = lines.iter().take(room).cloned().collect();
            if lines.len() > room
                && let Some(last) = kept.last_mut()
            {
                *last = clip_line(&format!("{last} …"), size, max_w, face);
                if !last.ends_with('…') {
                    last.push('…');
                }
            }
            return Fitted { size, lines: kept };
        }
        size = (size * 0.94).max(min);
    }
}

fn esc(s: &str) -> String {
    let mut o = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => o.push_str("&amp;"),
            '<' => o.push_str("&lt;"),
            '>' => o.push_str("&gt;"),
            '"' => o.push_str("&quot;"),
            '\'' => o.push_str("&#39;"),
            c if c.is_control() => {}
            c => o.push(c),
        }
    }
    o
}

/// Lines of a title, the first baseline at `y`.
fn title(svg: &mut String, f: &Fitted, x: f64, y: f64) {
    let lh = f.size * 1.12;
    for (i, line) in f.lines.iter().enumerate() {
        let _ = write!(
            svg,
            r#"<text class="{}" x="{x:.0}" y="{:.0}" font-size="{:.0}">{}</text>"#,
            Face::Title.class(),
            y + i as f64 * lh,
            f.size,
            esc(line)
        );
    }
}

/// The height a fitted block takes, cap height of the first line to the
/// baseline of the last.
fn block_h(f: &Fitted) -> f64 {
    f.size * 0.74 + (f.lines.len().saturating_sub(1)) as f64 * f.size * 1.12
}

// ── parts ───────────────────────────────────────────────────────────────────

/// Deterministic numbers from the seed: splitmix64.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    /// A number in `[lo, hi)`.
    fn range(&mut self, lo: f64, hi: f64) -> f64 {
        lo + (self.next() >> 11) as f64 / (1u64 << 53) as f64 * (hi - lo)
    }
    fn coin(&mut self) -> bool {
        self.next() & 1 == 1
    }
}

/// The margin on every side: a card's inner padding at card size.
const PAD: f64 = 112.0;
/// A hairline: about one pixel at card size.
const LINE: f64 = 4.0;
/// The dot grid's pitch; it reads as the map canvas's at card size.
const GRID: f64 = 56.0;
/// Radii: the 4 of a chip, the 10 of a card, at card size.
const R_CHIP: f64 = 22.0;
const R_CARD: f64 = 56.0;
const CHIP_H: f64 = 92.0;
const CHIP_TEXT: f64 = 48.0;
const CHIP_PAD: f64 = 34.0;
const CHIP_GAP: f64 = 20.0;
const KICKER_TEXT: f64 = 38.0;

/// The motif's tile: the accent, rounded as a Create tile's glyph is (9 of
/// 32), the motif half its size on it.
fn tile(svg: &mut String, name: &str, x: f64, y: f64, size: f64, a: &Accent) {
    let _ = write!(
        svg,
        r#"<rect x="{x:.0}" y="{y:.0}" width="{size:.0}" height="{size:.0}" rx="{:.0}" fill="{}"/>"#,
        size * 9.0 / 32.0,
        a.fill
    );
    let inner = motifs::motif(name)
        .or_else(|| motifs::motif(motifs::FALLBACK))
        .unwrap_or_default();
    let g = size * 0.5;
    let _ = write!(
        svg,
        r#"<g transform="translate({:.1} {:.1}) scale({:.3})" fill="{}">{inner}</g>"#,
        x + (size - g) / 2.0,
        y + (size - g) / 2.0,
        g / 16.0,
        a.on_fill
    );
}

/// The short accent rule and the section label, its baseline at `y`.
fn kicker(svg: &mut String, x: f64, y: f64, a: &Accent) {
    let _ = write!(
        svg,
        r#"<rect x="{x:.0}" y="{:.0}" width="48" height="8" rx="4" fill="{}"/><text class="k" x="{:.0}" y="{y:.0}" font-size="{KICKER_TEXT:.0}">{KICKER}</text>"#,
        y - KICKER_TEXT * 0.37 - 4.0,
        a.fill,
        x + 72.0,
    );
}

/// The terms that fit whole on one row from (`x`, `y`) within `max_w`, each
/// chip at most `max_chip` wide; the first is cut rather than left off, so a
/// row with terms is never empty. Returns how many were drawn.
fn chip_row(
    svg: &mut String,
    terms: &[String],
    x: f64,
    y: f64,
    max_w: f64,
    max_chip: f64,
) -> usize {
    let mut at = x;
    let mut n = 0;
    for t in terms {
        let Some(w) = chip(svg, t, at, y, (x + max_w - at).min(max_chip), n == 0) else {
            continue;
        };
        at += w + CHIP_GAP;
        n += 1;
    }
    n
}

/// One chip at (`x`, `y`) no wider than `room`, its width. A term that does
/// not fit whole is left off, unless `force`: then it is cut to the room, if
/// that leaves a few letters.
fn chip(svg: &mut String, term: &str, x: f64, y: f64, room: f64, force: bool) -> Option<f64> {
    let text_room = room - 2.0 * CHIP_PAD;
    let whole = width(term, CHIP_TEXT, Face::Chip);
    if whole > text_room && (!force || text_room < 3.0 * CHIP_TEXT) {
        return None;
    }
    let line = clip_line(term, CHIP_TEXT, text_room, Face::Chip);
    let w = width(&line, CHIP_TEXT, Face::Chip) + 2.0 * CHIP_PAD;
    let _ = write!(
        svg,
        r#"<rect class="chip" x="{x:.0}" y="{y:.0}" width="{w:.0}" height="{CHIP_H:.0}" rx="{R_CHIP:.0}" stroke-width="{LINE:.0}"/><text class="c" x="{:.0}" y="{:.0}" font-size="{CHIP_TEXT:.0}">{}</text>"#,
        x + CHIP_PAD,
        y + CHIP_H / 2.0 + CHIP_TEXT * 0.36,
        esc(&line)
    );
    Some(w)
}

// ── layouts ─────────────────────────────────────────────────────────────────

/// A large motif tile on one side, centred; on the other the label, the
/// topic and a row of chips, centred as a group.
fn tile_layout(svg: &mut String, c: &Cover, rng: &mut Rng) {
    let size = 256.0;
    let gap = 88.0;
    let left = rng.coin();
    let tx = if left { PAD } else { W - PAD - size };
    tile(svg, c.motif, tx, (H - size) / 2.0, size, c.accent);

    let x = if left { PAD + size + gap } else { PAD };
    let col = W - 2.0 * PAD - size - gap;
    let n = chip_row(&mut String::new(), c.terms, 0.0, 0.0, col, 560.0);
    let chips_h = if n > 0 { 56.0 + CHIP_H } else { 0.0 };
    let head = KICKER_TEXT * 0.74 + 48.0;
    let room = H - 2.0 * PAD - head - chips_h;
    let topic = fit(c.topic, Face::Title, col, room, 3, 124.0, 80.0);
    let group = head + block_h(&topic) + chips_h;
    let top = (H - group) / 2.0;
    kicker(svg, x, top + KICKER_TEXT * 0.74, c.accent);
    title(svg, &topic, x - 4.0, top + head + topic.size * 0.74);
    if n > 0 {
        let y = top + head + block_h(&topic) + 56.0;
        chip_row(svg, c.terms, x, y, col, 560.0);
    }
}

/// A card floating on the canvas, as tall as what it holds: the tile and the
/// label as its header, the topic, a hairline, and the chips.
fn card_layout(svg: &mut String, c: &Cover, rng: &mut Rng) {
    let (x0, x1) = (64.0, W - 64.0);
    let inset = 72.0;
    let (cx0, cx1) = (x0 + inset, x1 - inset);
    let size = 168.0;
    let n = chip_row(&mut String::new(), c.terms, 0.0, 0.0, cx1 - cx0, 560.0);
    let foot = if n > 0 { 44.0 + 40.0 + CHIP_H } else { 0.0 };
    let room = H - 2.0 * 64.0 - 2.0 * inset - size - 48.0 - foot;
    let topic = fit(c.topic, Face::Title, cx1 - cx0, room, 2, 112.0, 80.0);
    let h = 2.0 * inset + size + 48.0 + block_h(&topic) + foot;
    let y0 = (H - h) / 2.0;
    let _ = write!(
        svg,
        r#"<rect class="panel" x="{x0:.0}" y="{y0:.0}" width="{:.0}" height="{h:.0}" rx="{R_CARD:.0}" stroke-width="{LINE:.0}"/>"#,
        x1 - x0,
    );
    let cy0 = y0 + inset;
    // The tile leads the header on the left or closes it on the right.
    let left = rng.coin();
    let tx = if left { cx0 } else { cx1 - size };
    tile(svg, c.motif, tx, cy0, size, c.accent);
    let kx = if left { cx0 + size + 48.0 } else { cx0 };
    kicker(svg, kx, cy0 + size / 2.0 + KICKER_TEXT * 0.36, c.accent);
    let first = cy0 + size + 48.0 + topic.size * 0.74;
    title(svg, &topic, cx0 - 4.0, first);
    if n > 0 {
        let rule = first - topic.size * 0.74 + block_h(&topic) + 44.0;
        let _ = write!(
            svg,
            r#"<line class="rule" x1="{cx0:.0}" y1="{rule:.0}" x2="{cx1:.0}" y2="{rule:.0}" stroke-width="{LINE:.0}"/>"#
        );
        chip_row(svg, c.terms, cx0, rule + 40.0, cx1 - cx0, 560.0);
    }
}

/// The label and the topic at the top; under them the motif's tile as the
/// root of a small map, its links running to the terms as the map draws them.
fn map_layout(svg: &mut String, c: &Cover, rng: &mut Rng) {
    let top = PAD + KICKER_TEXT * 0.74;
    kicker(svg, PAD, top, c.accent);
    let topic = fit(c.topic, Face::Title, W - 2.0 * PAD, 236.0, 2, 112.0, 80.0);
    title(svg, &topic, PAD - 4.0, top + 44.0 + topic.size * 0.74);
    let map_top = top + 44.0 + block_h(&topic) + 64.0;

    let bottom = H - PAD;
    let size = 200.0_f64.min(bottom - map_top);
    let ry = map_top + (bottom - map_top - size) / 2.0;
    let mid = ry + size / 2.0;
    let shown: Vec<&String> = c.terms.iter().take(3).collect();
    if !shown.is_empty() {
        let gap = 24.0;
        let h = shown.len() as f64 * CHIP_H + (shown.len() - 1) as f64 * gap;
        let first = mid - h / 2.0;
        let mut links = String::new();
        let mut chips = String::new();
        for (i, t) in shown.iter().enumerate() {
            let y = first + i as f64 * (CHIP_H + gap);
            let x = 560.0 + rng.range(0.0, 72.0);
            if chip(&mut chips, t, x, y, W - PAD - x, true).is_none() {
                continue;
            }
            let (x1, y1, x2, y2) = (PAD + size, mid, x, y + CHIP_H / 2.0);
            let k = (x2 - x1) / 2.0;
            let _ = write!(
                links,
                r#"<path class="link" d="M{x1:.0} {y1:.0}C{:.0} {y1:.0} {:.0} {y2:.0} {x2:.0} {y2:.0}" stroke-width="{LINE:.0}"/>"#,
                x1 + k,
                x2 - k
            );
        }
        svg.push_str(&links);
        svg.push_str(&chips);
    }
    tile(svg, c.motif, PAD, ry, size, c.accent);
}

/// The tokens a cover draws with, from the sheet the app itself uses.
const STYLE: &str = "html,body{margin:0;height:100%;overflow:hidden;background:var(--bs-body-bg)}\
svg{display:block}\
text{font-family:var(--bs-font-sans-serif);font-kerning:normal}\
.bg{fill:var(--bs-body-bg)}\
.dot{fill:var(--bs-border-color)}\
.panel{fill:var(--bs-secondary-bg);stroke:var(--bs-border-color)}\
.chip{fill:var(--bs-secondary-bg);stroke:var(--bs-border-color)}\
.card .chip{fill:var(--bs-body-bg)}\
.rule{stroke:var(--bs-border-color-translucent)}\
.link{fill:none;stroke:var(--st-mm-link)}\
.t{fill:var(--bs-emphasis-color);font-weight:600;letter-spacing:-.01em}\
.k{fill:var(--bs-secondary-color);font-weight:600;letter-spacing:.07em}\
.c{fill:var(--bs-body-color);font-weight:500}";

/// The page: `<!doctype html>`, a CSP that allows nothing to load, the
/// studio's theme, and the SVG filling the viewport.
pub(crate) fn page(c: &Cover) -> String {
    let mut rng = Rng(c.seed);
    let mut svg = String::with_capacity(16 * 1024);
    // The grid starts at a different point per collection.
    let (ox, oy) = (rng.range(0.0, GRID), rng.range(0.0, GRID));
    let _ = write!(
        svg,
        r#"<defs><pattern id="dots" width="{GRID:.0}" height="{GRID:.0}" patternUnits="userSpaceOnUse" patternTransform="translate({ox:.0} {oy:.0})"><circle class="dot" cx="{h:.0}" cy="{h:.0}" r="3.5"/></pattern></defs><rect class="bg" width="{W}" height="{H}"/><rect width="{W}" height="{H}" fill="url(#dots)"/>"#,
        h = GRID / 2.0
    );
    let class = layout(c.layout).unwrap_or(LAYOUTS[0].0);
    let _ = write!(svg, r#"<g class="{class}">"#);
    match class {
        "card" => card_layout(&mut svg, c, &mut rng),
        "map" => map_layout(&mut svg, c, &mut rng),
        _ => tile_layout(&mut svg, c, &mut rng),
    }
    svg.push_str("</g>");
    let theme = match c.theme {
        Theme::Light => r#" data-bs-theme="light""#,
        Theme::Dark => "",
    };
    format!(
        r#"<!doctype html><html{theme}><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:"><title>{title}</title><style>{css}{STYLE}</style></head><body><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 900" width="100%" height="100%" preserveAspectRatio="xMidYMid slice" role="img" aria-label="{title}">{svg}</svg></body></html>"#,
        title = esc(c.topic),
        css = opennotebook_sdk::theme::CSS,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn lum(hex: &str) -> f64 {
        let h = hex.trim_start_matches('#');
        assert_eq!(h.len(), 6, "not a #rrggbb colour: {hex}");
        let v = u32::from_str_radix(h, 16).unwrap();
        let ch = |shift: u32| {
            let c = ((v >> shift) & 0xff) as f64 / 255.0;
            if c <= 0.03928 {
                c / 12.92
            } else {
                ((c + 0.055) / 1.055).powf(2.4)
            }
        };
        0.2126 * ch(16) + 0.7152 * ch(8) + 0.0722 * ch(0)
    }

    fn ratio(a: &str, b: &str) -> f64 {
        let (x, y) = (lum(a), lum(b));
        (x.max(y) + 0.05) / (x.min(y) + 0.05)
    }

    /// The `--name: value` pairs of the theme block that starts at `head`.
    fn tokens(head: &str) -> HashMap<String, String> {
        let css = opennotebook_sdk::theme::CSS;
        let start = css.find(head).unwrap_or_else(|| panic!("no block {head}")) + head.len();
        let body = &css[start..start + css[start..].find('}').unwrap()];
        body.split(';')
            .filter_map(|decl| {
                let decl = decl.trim();
                let decl = decl.rsplit("*/").next().unwrap_or(decl).trim();
                let (k, v) = decl.split_once(':')?;
                k.trim()
                    .starts_with("--")
                    .then(|| (k.trim().to_string(), v.trim().to_string()))
            })
            .collect()
    }

    fn themes() -> [(&'static str, HashMap<String, String>); 2] {
        [
            ("dark", tokens(":root {")),
            ("light", tokens(":root[data-bs-theme=\"light\"] {")),
        ]
    }

    #[test]
    fn every_accent_holds_its_motif_and_stands_on_every_surface_in_both_themes() {
        assert!((6..=8).contains(&ACCENTS.len()));
        assert_eq!(
            ACCENTS[0].fill, "#2563eb",
            "the first is the studio's primary"
        );
        let mut fails = Vec::new();
        for a in ACCENTS {
            let r = ratio(a.on_fill, a.fill);
            if r < 4.5 {
                fails.push(format!("{}: motif on fill {r:.2}:1", a.id));
            }
            // The tile is a graphic: 3:1 against what it sits on.
            for (theme, t) in themes() {
                for surface in ["--bs-body-bg", "--bs-secondary-bg"] {
                    let r = ratio(a.fill, &t[surface]);
                    if r < 3.0 {
                        fails.push(format!("{}: fill on {theme} {surface} {r:.2}:1", a.id));
                    }
                }
            }
        }
        assert!(fails.is_empty(), "{fails:#?}");
        let mut ids: Vec<_> = ACCENTS.iter().map(|a| a.id).collect();
        ids.sort();
        ids.dedup();
        assert_eq!(ids.len(), ACCENTS.len());
    }

    #[test]
    fn every_word_on_a_cover_reads_at_aa_in_both_themes() {
        // (text, surface) for the topic, the label and the chips, wherever
        // a layout puts them.
        let pairs = [
            ("--bs-emphasis-color", "--bs-body-bg"),
            ("--bs-emphasis-color", "--bs-secondary-bg"),
            ("--bs-secondary-color", "--bs-body-bg"),
            ("--bs-secondary-color", "--bs-secondary-bg"),
            ("--bs-body-color", "--bs-secondary-bg"),
            ("--bs-body-color", "--bs-body-bg"),
        ];
        let mut fails = Vec::new();
        for (theme, t) in themes() {
            for (fg, bg) in pairs {
                let r = ratio(&t[fg], &t[bg]);
                if r < 4.5 {
                    fails.push(format!("{theme}: {fg} on {bg} {r:.2}:1"));
                }
            }
        }
        assert!(fails.is_empty(), "{fails:#?}");
    }

    #[test]
    fn the_first_palettes_and_layouts_still_name_one() {
        for (old, new) in OLD_PALETTES {
            assert_eq!(accent(old).map(|a| a.id), Some(*new));
        }
        for (old, new) in OLD_LAYOUTS {
            assert_eq!(layout(old), Some(*new));
        }
        assert!(accent("neon").is_none());
        assert!(layout("collage").is_none());
    }

    #[test]
    fn the_theme_is_the_apps_and_dark_unless_chosen() {
        assert_eq!(Theme::parse("light"), Theme::Light);
        assert_eq!(Theme::parse(" Light "), Theme::Light);
        for s in ["", "dark", "auto", "x"] {
            assert_eq!(Theme::parse(s), Theme::Dark);
        }
        let terms = ["Mitosis".to_string()];
        let cover = |theme| Cover {
            topic: "Cells",
            terms: &terms,
            motif: "virus",
            accent: &ACCENTS[0],
            layout: "card",
            theme,
            seed: 7,
        };
        let dark = page(&cover(Theme::Dark));
        let light = page(&cover(Theme::Light));
        assert!(dark.starts_with("<!doctype html><html><head>"));
        assert!(light.starts_with(r#"<!doctype html><html data-bs-theme="light"><head>"#));
        assert!(
            dark.contains(opennotebook_sdk::theme::CSS),
            "the app's own tokens"
        );
        assert_eq!(
            dark.replacen("<html>", r#"<html data-bs-theme="light">"#, 1),
            light,
            "the theme switch is the only difference"
        );
    }

    #[test]
    fn a_word_too_long_for_a_line_is_broken_not_overflowed() {
        let lines = wrap(
            "Pneumonoultramicroscopicsilicovolcanoconiosis",
            100.0,
            500.0,
            Face::Title,
        );
        assert!(lines.len() > 1);
        for l in &lines {
            assert!(width(l, 100.0, Face::Title) <= 500.0, "{l}");
        }
    }

    #[test]
    fn fitting_shrinks_then_clips_with_an_ellipsis() {
        let short = fit("Cells", Face::Title, 1000.0, 300.0, 2, 100.0, 50.0);
        assert_eq!((short.size, short.lines.len()), (100.0, 1));
        let long = "word ".repeat(200);
        let f = fit(&long, Face::Title, 600.0, 400.0, 3, 120.0, 60.0);
        assert_eq!(f.size, 60.0);
        assert!(f.lines.len() <= 3);
        assert!(f.lines.last().unwrap().ends_with('…'), "{:?}", f.lines);
        for l in &f.lines {
            assert!(width(l, f.size, Face::Title) <= 600.0, "{l}");
        }
        assert!(f.lines.len() as f64 * f.size * 1.12 <= 400.0);
    }
}
