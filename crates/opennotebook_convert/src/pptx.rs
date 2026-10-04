//! PowerPoint (`.pptx`) to Markdown, one section per slide.
//!
//! Slides are taken in the order the presentation lists them, which is the
//! order a person sees, not the order of the part names: a deck whose slides
//! were reordered still has `slide1.xml` wherever it was first created.
//!
//! Each slide becomes `## Slide N: <title>`, followed by the text of its
//! shapes in the order they sit in the slide's shape tree, its tables, the
//! text of any SmartArt diagram, and finally the speaker notes under a
//! `Notes:` line. Text on the slide layout and master is template text
//! ("Click to add title") and is not read.

use std::collections::HashMap;

use crate::markdown::{self, Block, LINE_BREAK, Span};
use crate::ooxml::{Package, Rels};
use crate::xml::El;
use crate::{Error, InputKind};

const KIND: InputKind = InputKind::Powerpoint;

pub(crate) fn convert(bytes: &[u8]) -> Result<String, Error> {
    let mut pkg = Package::open(bytes, KIND)?;
    let main = pkg.main_part("ppt/presentation.xml")?;
    let presentation = pkg.read_xml(&main)?.ok_or(Error::NotValid(KIND))?;
    let rels = pkg.rels(&main)?;

    let slide_parts: Vec<String> = presentation
        .child("p:sldIdLst")
        .map(|list| {
            list.children_named("p:sldId")
                .filter_map(|s| s.attr("r:id"))
                .filter_map(|id| rels.get(id))
                .filter(|r| !r.external)
                .map(|r| r.target.clone())
                .collect()
        })
        .unwrap_or_default();

    let mut sections = Vec::new();
    for (index, part) in slide_parts.iter().enumerate() {
        // A slide listed but missing from the package is skipped rather than
        // failing the deck: the rest of the text is still there to read.
        let Some(slide) = pkg.read_xml(part)? else {
            continue;
        };
        let slide_rels = pkg.rels(part)?;
        let mut reader = SlideReader {
            rels: &slide_rels,
            title: None,
            blocks: Vec::new(),
            diagrams: Vec::new(),
        };
        if let Some(tree) = slide.path(&["p:cSld", "p:spTree"]) {
            reader.shapes(tree);
        }
        let SlideReader {
            title,
            mut blocks,
            diagrams,
            ..
        } = reader;
        for data_part in diagrams {
            if let Some(data) = pkg.read_xml(&data_part)? {
                blocks.extend(diagram_text(&data));
            }
        }

        let mut section = match title {
            Some(t) if !t.is_empty() => format!("## Slide {}: {t}", index + 1),
            _ => format!("## Slide {}", index + 1),
        };
        let body = markdown::render(&blocks);
        if !body.is_empty() {
            section.push_str("\n\n");
            section.push_str(&body);
        }
        if let Some(notes) = notes(&mut pkg, &slide_rels)? {
            section.push_str("\n\nNotes:\n\n");
            section.push_str(&notes);
        }
        sections.push(section);
    }
    Ok(sections.join("\n\n"))
}

/// The speaker notes of a slide, from the body placeholder of its notes
/// page. The notes page also carries a slide image and a slide number, which
/// are not notes.
fn notes(pkg: &mut Package<'_>, slide_rels: &Rels) -> Result<Option<String>, Error> {
    let Some(rel) = slide_rels
        .values()
        .find(|r| r.is("notesSlide") && !r.external)
    else {
        return Ok(None);
    };
    let part = rel.target.clone();
    let Some(page) = pkg.read_xml(&part)? else {
        return Ok(None);
    };
    let rels = pkg.rels(&part)?;
    let mut shapes = Vec::new();
    page.find_all("p:sp", &mut shapes);
    let mut blocks = Vec::new();
    for sp in shapes {
        if placeholder_type(sp) != Some(Some("body")) {
            continue;
        }
        if let Some(body) = sp.child("p:txBody") {
            text_body(body, &rels, false, &mut blocks);
        }
    }
    let text = markdown::render(&blocks);
    Ok((!text.trim().is_empty()).then_some(text))
}

struct SlideReader<'a> {
    rels: &'a Rels,
    title: Option<String>,
    blocks: Vec<Block>,
    /// SmartArt data parts, read once the shape walk is done.
    diagrams: Vec<String>,
}

impl SlideReader<'_> {
    fn shapes(&mut self, tree: &El) {
        for el in tree.elements() {
            match el.name.as_str() {
                "p:sp" => self.shape(el),
                "p:grpSp" => self.shapes(el),
                "p:graphicFrame" => {
                    if let Some(tbl) = el.find("a:tbl") {
                        let rows = table(tbl, self.rels);
                        if !rows.is_empty() {
                            self.blocks.push(Block::Table(rows));
                        }
                    } else if let Some(ids) = el.find("dgm:relIds")
                        && let Some(rel) = ids.attr("r:dm").and_then(|id| self.rels.get(id))
                        && !rel.external
                    {
                        self.diagrams.push(rel.target.clone());
                    }
                }
                _ => {}
            }
        }
    }

    fn shape(&mut self, sp: &El) {
        let Some(body) = sp.child("p:txBody") else {
            return;
        };
        let placeholder = placeholder_type(sp);
        match placeholder {
            // Date and slide number are filled in by PowerPoint, not written
            // by the author.
            Some(Some("dt" | "sldNum")) => {}
            Some(Some("title" | "ctrTitle")) if self.title.is_none() => {
                let title = body
                    .children_named("a:p")
                    .map(|p| markdown::one_line(&markdown::plain(&paragraph_spans(p, self.rels))))
                    .filter(|t| !t.is_empty())
                    .collect::<Vec<_>>()
                    .join(" ");
                self.title = Some(title);
            }
            _ => {
                // Body and content placeholders take their bullets from the
                // slide master, so their paragraphs are list items unless they
                // say otherwise. A free text box has no bullets by default.
                let bulleted = matches!(placeholder, Some(None | Some("body" | "obj")));
                text_body(body, self.rels, bulleted, &mut self.blocks);
            }
        }
    }
}

/// `None` for a shape that is not a placeholder, `Some(type)` for one that is,
/// with `Some(None)` for a placeholder that has no type (a content
/// placeholder, which defaults to body).
fn placeholder_type(sp: &El) -> Option<Option<&str>> {
    let ph = sp.path(&["p:nvSpPr", "p:nvPr", "p:ph"])?;
    Some(ph.attr("type"))
}

/// Paragraphs of a text body as blocks: list items where the paragraph has a
/// bullet (explicitly, or by default in a body placeholder), plain paragraphs
/// otherwise.
fn text_body(body: &El, rels: &Rels, bulleted: bool, out: &mut Vec<Block>) {
    // Running numbers for auto-numbered paragraphs, by level.
    let mut counters: HashMap<usize, u32> = HashMap::new();
    for p in body.children_named("a:p") {
        let ppr = p.child("a:pPr");
        let level = ppr
            .and_then(|p| p.attr("lvl"))
            .and_then(|v| v.parse::<usize>().ok())
            .unwrap_or(0)
            .min(8);
        let spans = paragraph_spans(p, rels);
        let text = markdown::paragraph_text(&markdown::inline(&spans));
        if text.is_empty() {
            continue;
        }
        let auto = ppr.and_then(|p| p.child("a:buAutoNum"));
        let has_bullet = match ppr {
            Some(p) if p.child("a:buNone").is_some() => false,
            Some(p) if p.child("a:buChar").is_some() || p.child("a:buBlip").is_some() => true,
            _ => auto.is_some() || bulleted,
        };
        counters.retain(|&l, _| l <= level);
        if !has_bullet {
            counters.remove(&level);
            out.push(Block::Paragraph(text));
            continue;
        }
        let ordered = auto.map(|a| {
            let start = a
                .attr("startAt")
                .and_then(|v| v.parse::<u32>().ok())
                .unwrap_or(1);
            let n = counters.entry(level).or_insert(start.saturating_sub(1));
            *n += 1;
            *n
        });
        if ordered.is_none() {
            counters.remove(&level);
        }
        out.push(Block::Item {
            depth: level,
            ordered,
            text,
        });
    }
}

fn paragraph_spans(p: &El, rels: &Rels) -> Vec<Span> {
    let mut spans: Vec<Span> = Vec::new();
    for el in p.elements() {
        let (text, rpr) = match el.name.as_str() {
            "a:r" | "a:fld" => (
                el.child("a:t").map(El::own_text).unwrap_or_default(),
                el.child("a:rPr"),
            ),
            "a:br" => (LINE_BREAK.to_string(), None),
            _ => continue,
        };
        if text.is_empty() {
            continue;
        }
        let flag = |name: &str| {
            rpr.and_then(|r| r.attr(name))
                .is_some_and(|v| v == "1" || v == "true")
        };
        let link = rpr
            .and_then(|r| r.child("a:hlinkClick"))
            .and_then(|h| h.attr("r:id"))
            .and_then(|id| rels.get(id))
            .filter(|r| r.external)
            .map(|r| r.target.clone());
        spans.push(Span {
            text,
            bold: flag("b"),
            italic: flag("i"),
            link,
        });
    }
    spans
}

/// A slide table. PowerPoint writes every grid cell, marking the ones covered
/// by a merge with `hMerge` or `vMerge`, so covered cells are simply empty
/// and the rows stay aligned.
fn table(tbl: &El, rels: &Rels) -> Vec<Vec<String>> {
    let mut rows = Vec::new();
    for tr in tbl.children_named("a:tr") {
        let mut row = Vec::new();
        for tc in tr.children_named("a:tc") {
            let covered = ["hMerge", "vMerge"]
                .iter()
                .any(|a| matches!(tc.attr(a), Some("1" | "true")));
            if covered {
                row.push(String::new());
                continue;
            }
            let lines: Vec<String> = tc
                .child("a:txBody")
                .map(|b| {
                    b.children_named("a:p")
                        .map(|p| markdown::inline(&paragraph_spans(p, rels)))
                        .collect()
                })
                .unwrap_or_default();
            row.push(markdown::cell(&lines.join(&LINE_BREAK.to_string())));
        }
        rows.push(row);
    }
    rows
}

/// The text of a SmartArt diagram, one item per node. The data part holds the
/// nodes the author typed; the drawing part next to it repeats the same text
/// laid out, so only the data part is read.
fn diagram_text(data: &El) -> Vec<Block> {
    let mut paragraphs = Vec::new();
    data.find_all("a:p", &mut paragraphs);
    paragraphs
        .into_iter()
        .map(|p| markdown::paragraph_text(&markdown::plain(&paragraph_spans(p, &Rels::new()))))
        .filter(|t| !t.is_empty())
        .map(|text| Block::Item {
            depth: 0,
            ordered: None,
            text,
        })
        .collect()
}
