//! Word (`.docx`) to Markdown, read from the WordprocessingML parts directly.
//!
//! The main document is walked in order. Paragraphs become headings, list
//! items or plain paragraphs depending on their style and numbering; tables
//! become GFM tables; text boxes, which Word anchors inside a run, are written
//! as their own blocks right after the paragraph that holds them. Footnotes
//! and endnotes are appended at the end as Markdown footnotes, referenced from
//! where they were in the text.
//!
//! Tracked changes are read the way the document currently reads: deleted
//! text is left out and inserted text is kept, as if every change had been
//! accepted.

use std::collections::{HashMap, HashSet};

use crate::markdown::{self, Block, LINE_BREAK, Span};
use crate::ooxml::{Package, Rels};
use crate::xml::El;
use crate::{Error, InputKind};

/// How far a chain of `basedOn` styles is followed. Real chains are a few
/// deep; the limit only stops a style that is based on itself.
const MAX_STYLE_CHAIN: usize = 16;

pub(crate) fn convert(bytes: &[u8]) -> Result<String, Error> {
    let mut pkg = Package::open(bytes, InputKind::Word)?;
    let main = pkg.main_part("word/document.xml")?;
    let document = pkg
        .read_xml(&main)?
        .ok_or(Error::NotValid(InputKind::Word))?;
    let rels = pkg.rels(&main)?;

    let part_of = |kind: &str| {
        rels.values()
            .find(|r| r.is(kind) && !r.external)
            .map(|r| r.target.clone())
    };
    let styles = match part_of("styles") {
        Some(part) => pkg.read_xml(&part)?.map(|x| Styles::read(&x)),
        None => None,
    }
    .unwrap_or_default();
    let numbering = match part_of("numbering") {
        Some(part) => pkg.read_xml(&part)?.map(|x| Numbering::read(&x)),
        None => None,
    }
    .unwrap_or_default();
    let mut notes = HashMap::new();
    for (kind, part) in [
        (NoteKind::Foot, part_of("footnotes")),
        (NoteKind::End, part_of("endnotes")),
    ] {
        let Some(part) = part else { continue };
        let Some(root) = pkg.read_xml(&part)? else {
            continue;
        };
        // Notes have relationships of their own, for links inside them.
        let note_rels = pkg.rels(&part)?;
        notes.insert(kind, (root, note_rels));
    }

    let body = document
        .child("w:body")
        .ok_or(Error::NotValid(InputKind::Word))?;
    let mut reader = Reader {
        styles: &styles,
        numbering: &numbering,
        rels: &rels,
        counters: HashMap::new(),
        fields: Vec::new(),
        note_refs: Vec::new(),
        note_labels: HashMap::new(),
    };
    let mut blocks = Vec::new();
    reader.blocks(body, &mut blocks);
    let mut out = markdown::render(&blocks);

    let notes_md = reader.notes(&notes);
    if !notes_md.is_empty() {
        out.push_str("\n\n");
        out.push_str(&notes_md);
    }
    Ok(out)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum NoteKind {
    Foot,
    End,
}

/// What the converter needs from `styles.xml`.
#[derive(Debug, Default)]
struct Styles {
    by_id: HashMap<String, Style>,
}

#[derive(Debug, Default)]
struct Style {
    name: String,
    based_on: Option<String>,
    outline: Option<usize>,
    numbering: Option<(String, u32)>,
    bold: Option<bool>,
    italic: Option<bool>,
}

impl Styles {
    fn read(root: &El) -> Self {
        let mut by_id = HashMap::new();
        for s in root.children_named("w:style") {
            let Some(id) = s.attr("w:styleId") else {
                continue;
            };
            let ppr = s.child("w:pPr");
            let rpr = s.child("w:rPr");
            let style = Style {
                name: s
                    .child("w:name")
                    .and_then(|n| n.attr("w:val"))
                    .unwrap_or_default()
                    .to_ascii_lowercase(),
                based_on: s
                    .child("w:basedOn")
                    .and_then(|b| b.attr("w:val"))
                    .map(str::to_string),
                outline: ppr
                    .and_then(|p| p.child("w:outlineLvl"))
                    .and_then(|o| o.attr("w:val"))
                    .and_then(|v| v.parse().ok()),
                numbering: ppr.and_then(num_pr),
                bold: rpr.and_then(|r| toggle(r, "w:b")),
                italic: rpr.and_then(|r| toggle(r, "w:i")),
            };
            by_id.insert(id.to_string(), style);
        }
        Self { by_id }
    }

    /// The style and the styles it is based on, nearest first.
    fn chain<'s>(&'s self, id: &'s str) -> Vec<(&'s str, &'s Style)> {
        let mut out = Vec::new();
        let mut next = Some(id);
        while let Some(id) = next {
            let Some(style) = self.by_id.get(id) else {
                break;
            };
            if out.len() >= MAX_STYLE_CHAIN {
                break;
            }
            out.push((id, style));
            next = style.based_on.as_deref();
        }
        out
    }

    /// The Markdown heading level a paragraph style stands for, if any.
    /// Built-in heading styles are recognised by name, which survives
    /// localisation of the id; anything else counts if it, or a style it is
    /// based on, carries an outline level, which is what Word itself uses to
    /// build a table of contents.
    fn heading_level(&self, id: &str) -> Option<usize> {
        let by_name = |name: &str| -> Option<usize> {
            if name == "title" {
                return Some(1);
            }
            let n = name.strip_prefix("heading")?.trim();
            n.parse::<usize>().ok().filter(|n| (1..=9).contains(n))
        };
        if let Some(level) = by_name(&id.to_ascii_lowercase()) {
            return Some(level.min(6));
        }
        for (_, style) in self.chain(id) {
            if let Some(level) = by_name(&style.name) {
                return Some(level.min(6));
            }
            if let Some(outline) = style.outline
                && outline < 6
            {
                return Some(outline + 1);
            }
        }
        None
    }

    fn numbering(&self, id: &str) -> Option<(String, u32)> {
        self.chain(id)
            .into_iter()
            .find_map(|(_, s)| s.numbering.clone())
    }

    fn emphasis(&self, id: &str) -> (Option<bool>, Option<bool>) {
        let chain = self.chain(id);
        (
            chain.iter().find_map(|(_, s)| s.bold),
            chain.iter().find_map(|(_, s)| s.italic),
        )
    }
}

/// A list paragraph's numbering reference: `(numId, ilvl)`.
fn num_pr(ppr: &El) -> Option<(String, u32)> {
    let np = ppr.child("w:numPr")?;
    let id = np.child("w:numId").and_then(|n| n.attr("w:val"))?;
    let level = np
        .child("w:ilvl")
        .and_then(|l| l.attr("w:val"))
        .and_then(|v| v.parse().ok())
        .unwrap_or(0);
    Some((id.to_string(), level))
}

/// A boolean run property such as `<w:b/>`, which is on when present unless
/// its value says otherwise.
fn toggle(rpr: &El, name: &str) -> Option<bool> {
    let el = rpr.child(name)?;
    Some(!matches!(el.attr("w:val"), Some("0" | "false" | "off")))
}

/// What the converter needs from `numbering.xml`: whether each list level is
/// bulleted or numbered, and where its numbering starts.
#[derive(Debug, Default)]
struct Numbering {
    /// `numId` to its abstract definition and per-level overrides.
    nums: HashMap<String, (String, HashMap<u32, Level>)>,
    abstracts: HashMap<String, HashMap<u32, Level>>,
}

#[derive(Debug, Clone, Copy)]
struct Level {
    ordered: bool,
    start: u32,
}

impl Default for Level {
    fn default() -> Self {
        Self {
            ordered: false,
            start: 1,
        }
    }
}

fn read_levels<'a>(parent: impl Iterator<Item = &'a El>) -> HashMap<u32, Level> {
    let mut levels = HashMap::new();
    for lvl in parent {
        let Some(i) = lvl.attr("w:ilvl").and_then(|v| v.parse().ok()) else {
            continue;
        };
        let format = lvl
            .child("w:numFmt")
            .and_then(|f| f.attr("w:val"))
            .unwrap_or("bullet");
        let start = lvl
            .child("w:start")
            .and_then(|s| s.attr("w:val"))
            .and_then(|v| v.parse().ok())
            .unwrap_or(1);
        levels.insert(
            i,
            Level {
                ordered: !matches!(format, "bullet" | "none"),
                start,
            },
        );
    }
    levels
}

impl Numbering {
    fn read(root: &El) -> Self {
        let mut abstracts = HashMap::new();
        for a in root.children_named("w:abstractNum") {
            if let Some(id) = a.attr("w:abstractNumId") {
                abstracts.insert(id.to_string(), read_levels(a.children_named("w:lvl")));
            }
        }
        let mut nums = HashMap::new();
        for n in root.children_named("w:num") {
            let (Some(id), Some(abs)) = (
                n.attr("w:numId"),
                n.child("w:abstractNumId").and_then(|a| a.attr("w:val")),
            ) else {
                continue;
            };
            let mut overrides = HashMap::new();
            for o in n.children_named("w:lvlOverride") {
                let Some(i) = o.attr("w:ilvl").and_then(|v| v.parse().ok()) else {
                    continue;
                };
                if let Some(level) = read_levels(o.children_named("w:lvl")).remove(&i) {
                    overrides.insert(i, level);
                } else if let Some(start) = o
                    .child("w:startOverride")
                    .and_then(|s| s.attr("w:val"))
                    .and_then(|v| v.parse().ok())
                {
                    overrides.insert(
                        i,
                        Level {
                            start,
                            ..self_level(&abstracts, abs, i)
                        },
                    );
                }
            }
            nums.insert(id.to_string(), (abs.to_string(), overrides));
        }
        Self { nums, abstracts }
    }

    fn level(&self, num: &str, ilvl: u32) -> Level {
        match self.nums.get(num) {
            Some((abs, overrides)) => overrides
                .get(&ilvl)
                .copied()
                .unwrap_or_else(|| self_level(&self.abstracts, abs, ilvl)),
            None => Level::default(),
        }
    }
}

fn self_level(abstracts: &HashMap<String, HashMap<u32, Level>>, abs: &str, ilvl: u32) -> Level {
    abstracts
        .get(abs)
        .and_then(|l| l.get(&ilvl))
        .copied()
        .unwrap_or_default()
}

/// A complex field (`fldChar begin … separate … end`) that is open at the
/// current position. Only its result is document text; the instruction is
/// collected to recognise `HYPERLINK` fields, whose result becomes a link.
struct Field {
    instruction: String,
    in_result: bool,
    /// Index in the current paragraph's spans where the result began.
    start: usize,
}

/// Inline content gathered from one paragraph.
#[derive(Default)]
struct Inline {
    spans: Vec<Span>,
    /// Blocks found inside the paragraph's runs (text boxes), written after it.
    anchored: Vec<Block>,
    /// Set where a field result begins, so the next text starts a span of
    /// its own and the result can later be turned into a link on its own.
    boundary: bool,
}

#[derive(Clone, Copy, Default)]
struct Format {
    bold: bool,
    italic: bool,
}

struct Reader<'a> {
    styles: &'a Styles,
    numbering: &'a Numbering,
    rels: &'a Rels,
    /// Running numbers for ordered lists, by `(numId, level)`, so a list that
    /// continues after an interruption keeps counting as Word does.
    counters: HashMap<(String, u32), u32>,
    fields: Vec<Field>,
    note_refs: Vec<(NoteKind, String)>,
    note_labels: HashMap<(NoteKind, String), String>,
}

impl Reader<'_> {
    /// Block-level content: the body, a table cell, a text box or a note.
    fn blocks(&mut self, parent: &El, out: &mut Vec<Block>) {
        for el in parent.elements() {
            match el.name.as_str() {
                "w:p" => self.paragraph(el, out),
                "w:tbl" => {
                    let rows = self.table(el);
                    if !rows.is_empty() {
                        out.push(Block::Table(rows));
                    }
                }
                // Deleted and moved-away content is not part of the document
                // as it now reads.
                "w:del" | "w:moveFrom" | "w:sectPr" => {}
                // Content controls, custom XML, and tracked insertions all
                // wrap ordinary blocks; anything else holds none and costs
                // nothing to look through.
                "w:sdtPr" | "w:sdtEndPr" => {}
                _ => self.blocks(el, out),
            }
        }
    }

    fn paragraph(&mut self, p: &El, out: &mut Vec<Block>) {
        for field in &mut self.fields {
            field.start = 0;
        }
        let mut inline = Inline::default();
        self.inline(p, &mut inline, Format::default(), None);

        let ppr = p.child("w:pPr");
        let style = ppr
            .and_then(|p| p.child("w:pStyle"))
            .and_then(|s| s.attr("w:val"));
        let direct_outline = ppr
            .and_then(|p| p.child("w:outlineLvl"))
            .and_then(|o| o.attr("w:val"))
            .and_then(|v| v.parse::<usize>().ok())
            .filter(|&o| o < 6)
            .map(|o| o + 1);
        let heading = style
            .and_then(|s| self.styles.heading_level(s))
            .or(direct_outline);
        let numbering = ppr
            .and_then(num_pr)
            .or_else(|| style.and_then(|s| self.styles.numbering(s)))
            .filter(|(id, _)| id != "0");

        if let Some(level) = heading {
            let text = markdown::one_line(&markdown::plain(&inline.spans));
            if !text.is_empty() {
                out.push(Block::Heading(level, text));
            }
        } else {
            let text = markdown::paragraph_text(&markdown::inline(&inline.spans));
            if !text.is_empty() {
                match numbering {
                    Some((num, ilvl)) => {
                        let ordered = self.list_number(&num, ilvl);
                        out.push(Block::Item {
                            depth: ilvl as usize,
                            ordered,
                            text,
                        });
                    }
                    None => out.push(Block::Paragraph(text)),
                }
            }
        }
        out.append(&mut inline.anchored);
    }

    /// The number an ordered list item shows, or `None` for a bullet.
    fn list_number(&mut self, num: &str, ilvl: u32) -> Option<u32> {
        // A shallower item restarts the levels below it, as in 1. a. b. 2. a.
        self.counters.retain(|(n, l), _| n != num || *l <= ilvl);
        let level = self.numbering.level(num, ilvl);
        if !level.ordered {
            return None;
        }
        let counter = self
            .counters
            .entry((num.to_string(), ilvl))
            .or_insert(level.start.saturating_sub(1));
        *counter += 1;
        Some(*counter)
    }

    /// Inline content of a paragraph or of anything wrapping runs inside it.
    fn inline(&mut self, parent: &El, out: &mut Inline, format: Format, link: Option<&str>) {
        for el in parent.elements() {
            match el.name.as_str() {
                "w:r" => self.run(el, out, format, link),
                "w:hyperlink" => {
                    let url = el
                        .attr("r:id")
                        .and_then(|id| self.rels.get(id))
                        .filter(|r| r.external)
                        .map(|r| r.target.clone());
                    self.inline(el, out, format, url.as_deref().or(link));
                }
                "w:fldSimple" => {
                    let url = el.attr("w:instr").and_then(hyperlink_target);
                    self.inline(el, out, format, url.as_deref().or(link));
                }
                "w:del" | "w:moveFrom" | "w:pPr" | "w:rPr" | "w:sdtPr" | "w:sdtEndPr" => {}
                // Office Math keeps its characters in `m:t`.
                "m:t" => push(out, &el.own_text(), format, link),
                _ => self.inline(el, out, format, link),
            }
        }
    }

    fn run(&mut self, r: &El, out: &mut Inline, inherited: Format, link: Option<&str>) {
        let rpr = r.child("w:rPr");
        let (style_bold, style_italic) = rpr
            .and_then(|p| p.child("w:rStyle"))
            .and_then(|s| s.attr("w:val"))
            .map(|s| self.styles.emphasis(s))
            .unwrap_or_default();
        let format = Format {
            bold: rpr
                .and_then(|p| toggle(p, "w:b"))
                .or(style_bold)
                .unwrap_or(inherited.bold),
            italic: rpr
                .and_then(|p| toggle(p, "w:i"))
                .or(style_italic)
                .unwrap_or(inherited.italic),
        };
        // Text between a field's `begin` and `separate` is its instruction,
        // not something the reader sees.
        let hidden = |fields: &[Field]| fields.last().is_some_and(|f| !f.in_result);

        for el in r.elements() {
            match el.name.as_str() {
                "w:t" if !hidden(&self.fields) => push(out, &el.own_text(), format, link),
                "w:tab" | "w:ptab" if !hidden(&self.fields) => push(out, "\t", format, link),
                "w:br" | "w:cr" if !hidden(&self.fields) => {
                    push(out, &LINE_BREAK.to_string(), format, link)
                }
                "w:noBreakHyphen" if !hidden(&self.fields) => push(out, "-", format, link),
                "w:sym" if !hidden(&self.fields) => {
                    // Symbol-font characters live in the private use area and
                    // mean nothing outside that font; anything else is a real
                    // character.
                    if let Some(c) = el
                        .attr("w:char")
                        .and_then(|h| u32::from_str_radix(h, 16).ok())
                        .filter(|c| !(0xE000..=0xF8FF).contains(c))
                        .and_then(char::from_u32)
                    {
                        push(out, &c.to_string(), format, link);
                    }
                }
                "w:fldChar" => match el.attr("w:fldCharType") {
                    Some("begin") => self.fields.push(Field {
                        instruction: String::new(),
                        in_result: false,
                        start: out.spans.len(),
                    }),
                    Some("separate") => {
                        if let Some(f) = self.fields.last_mut() {
                            f.in_result = true;
                            f.start = out.spans.len();
                            out.boundary = true;
                        }
                    }
                    Some("end") => {
                        if let Some(f) = self.fields.pop()
                            && let Some(url) = hyperlink_target(&f.instruction)
                        {
                            for span in out.spans.iter_mut().skip(f.start) {
                                if span.link.is_none() {
                                    span.link = Some(url.clone());
                                }
                            }
                        }
                    }
                    _ => {}
                },
                "w:instrText" => {
                    if let Some(f) = self.fields.last_mut()
                        && !f.in_result
                    {
                        f.instruction.push_str(&el.own_text());
                    }
                }
                "w:footnoteReference" | "w:endnoteReference" => {
                    let kind = if el.name == "w:footnoteReference" {
                        NoteKind::Foot
                    } else {
                        NoteKind::End
                    };
                    if let Some(id) = el.attr("w:id") {
                        let label = self.note_label(kind, id);
                        push(out, &format!("[^{label}]"), Format::default(), None);
                    }
                }
                "w:rPr" | "w:delText" | "w:delInstrText" => {}
                // Drawings, VML shapes and embedded objects: their only text
                // is in text boxes, which are whole blocks of their own.
                _ => {
                    let mut boxes = Vec::new();
                    el.find_all("w:txbxContent", &mut boxes);
                    for b in boxes {
                        self.blocks(b, &mut out.anchored);
                    }
                }
            }
        }
    }

    fn note_label(&mut self, kind: NoteKind, id: &str) -> String {
        let key = (kind, id.to_string());
        if let Some(label) = self.note_labels.get(&key) {
            return label.clone();
        }
        let label = (self.note_labels.len() + 1).to_string();
        self.note_labels.insert(key.clone(), label.clone());
        self.note_refs.push(key);
        label
    }

    /// The footnote and endnote definitions: those referenced from the text
    /// in the order they were referenced, then any that nothing references,
    /// so no note's text is lost either way.
    fn notes(&mut self, notes: &HashMap<NoteKind, (El, Rels)>) -> String {
        let mut order = self.note_refs.clone();
        let referenced: HashSet<_> = order.iter().cloned().collect();
        for kind in [NoteKind::Foot, NoteKind::End] {
            let Some((root, _)) = notes.get(&kind) else {
                continue;
            };
            for note in root.elements() {
                // Separator "notes" hold the line Word draws above the notes.
                if note.attr("w:type").is_some_and(|t| t != "normal") {
                    continue;
                }
                if let Some(id) = note.attr("w:id") {
                    let key = (kind, id.to_string());
                    if !referenced.contains(&key) {
                        self.note_label(kind, id);
                        order.push(key);
                    }
                }
            }
        }

        let mut out = Vec::new();
        for (kind, id) in order {
            let Some((root, rels)) = notes.get(&kind) else {
                continue;
            };
            let Some(note) = root
                .elements()
                .find(|n| n.attr("w:id") == Some(id.as_str()))
            else {
                continue;
            };
            let mut reader = Reader {
                styles: self.styles,
                numbering: self.numbering,
                rels,
                counters: HashMap::new(),
                fields: Vec::new(),
                note_refs: Vec::new(),
                note_labels: HashMap::new(),
            };
            let mut blocks = Vec::new();
            reader.blocks(note, &mut blocks);
            let text = markdown::render(&blocks);
            if text.trim().is_empty() {
                continue;
            }
            let label = &self.note_labels[&(kind, id)];
            // Later lines of a note are indented so they stay in it.
            out.push(format!(
                "[^{label}]: {}",
                text.trim().replace('\n', "\n    ")
            ));
        }
        out.join("\n\n")
    }

    /// A table as rows of rendered cells. Horizontally merged cells are
    /// written as the merged cell followed by empty ones, and a vertically
    /// merged cell's continuation is empty, which keeps every row aligned
    /// with the grid.
    fn table(&mut self, tbl: &El) -> Vec<Vec<String>> {
        let mut rows = Vec::new();
        for tr in descend(tbl, "w:tr", "w:tbl") {
            let mut row = Vec::new();
            let grid = |name: &str| {
                tr.child("w:trPr")
                    .and_then(|p| p.child(name))
                    .and_then(|g| g.attr("w:val"))
                    .and_then(|v| v.parse::<usize>().ok())
                    .unwrap_or(0)
                    .min(64)
            };
            row.extend(std::iter::repeat_n(String::new(), grid("w:gridBefore")));
            for tc in descend(tr, "w:tc", "w:tbl") {
                let tcpr = tc.child("w:tcPr");
                let span = tcpr
                    .and_then(|p| p.child("w:gridSpan"))
                    .and_then(|g| g.attr("w:val"))
                    .and_then(|v| v.parse::<usize>().ok())
                    .unwrap_or(1)
                    .clamp(1, 64);
                let continued = tcpr
                    .and_then(|p| p.child("w:hMerge"))
                    .is_some_and(|m| m.attr("w:val") != Some("restart"));
                let text = if continued {
                    String::new()
                } else {
                    self.cell_text(tc)
                };
                row.push(text);
                row.extend(std::iter::repeat_n(String::new(), span - 1));
            }
            row.extend(std::iter::repeat_n(String::new(), grid("w:gridAfter")));
            rows.push(row);
        }
        rows
    }

    fn cell_text(&mut self, tc: &El) -> String {
        let mut blocks = Vec::new();
        self.blocks(tc, &mut blocks);
        let mut lines = Vec::new();
        for block in blocks {
            match block {
                Block::Heading(_, t) | Block::Paragraph(t) => lines.push(t),
                Block::Item { ordered, text, .. } => lines.push(match ordered {
                    Some(n) => format!("{n}. {text}"),
                    None => format!("- {text}"),
                }),
                // A table inside a cell is flattened: one line per row,
                // cells separated by slashes.
                Block::Table(rows) => {
                    for r in rows {
                        let cells: Vec<_> = r.into_iter().filter(|c| !c.is_empty()).collect();
                        if !cells.is_empty() {
                            lines.push(cells.join(" / ").replace("\\|", "|"));
                        }
                    }
                }
            }
        }
        markdown::cell(&lines.join(&LINE_BREAK.to_string()))
    }
}

/// Elements named `want` under `parent`, looking through the wrappers that may
/// sit between them (content controls, custom XML, tracked insertions) but not
/// into a nested `stop` element such as an inner table.
fn descend<'a>(parent: &'a El, want: &str, stop: &str) -> Vec<&'a El> {
    let mut out = Vec::new();
    for el in parent.elements() {
        if el.name == want {
            out.push(el);
        } else if el.name != stop && !matches!(el.name.as_str(), "w:del" | "w:moveFrom") {
            out.extend(descend(el, want, stop));
        }
    }
    out
}

fn push(out: &mut Inline, text: &str, format: Format, link: Option<&str>) {
    if text.is_empty() {
        return;
    }
    let link = link.map(str::to_string);
    let boundary = std::mem::take(&mut out.boundary);
    if !boundary
        && let Some(last) = out.spans.last_mut()
        && last.bold == format.bold
        && last.italic == format.italic
        && last.link == link
    {
        last.text.push_str(text);
        return;
    }
    out.spans.push(Span {
        text: text.to_string(),
        bold: format.bold,
        italic: format.italic,
        link,
    });
}

/// The URL of a `HYPERLINK "…"` field instruction. Links to a bookmark in the
/// same document (`\l`) have no URL worth writing and are left as text.
fn hyperlink_target(instruction: &str) -> Option<String> {
    let rest = instruction.trim().strip_prefix("HYPERLINK")?;
    let mut tokens = Vec::new();
    let mut chars = rest.chars().peekable();
    while let Some(&c) = chars.peek() {
        if c.is_whitespace() {
            chars.next();
        } else if c == '"' {
            chars.next();
            tokens.push(chars.by_ref().take_while(|&c| c != '"').collect::<String>());
        } else {
            let mut t = String::new();
            while let Some(&c) = chars.peek() {
                if c.is_whitespace() {
                    break;
                }
                t.push(c);
                chars.next();
            }
            tokens.push(t);
        }
    }
    let mut iter = tokens.into_iter();
    while let Some(t) = iter.next() {
        if t.starts_with('\\') {
            // Switches with an argument consume it; `\l` names a bookmark.
            if matches!(t.as_str(), "\\l" | "\\m" | "\\n" | "\\o" | "\\t") {
                iter.next();
            }
            continue;
        }
        if !t.is_empty() {
            return Some(t);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::hyperlink_target;

    #[test]
    fn hyperlink_fields_yield_their_url() {
        assert_eq!(
            hyperlink_target(r#" HYPERLINK "https://example.com/x" \o "tip" "#).as_deref(),
            Some("https://example.com/x")
        );
        assert_eq!(hyperlink_target(r#"HYPERLINK \l "_Toc1""#), None);
        assert_eq!(hyperlink_target("PAGE"), None);
    }
}
