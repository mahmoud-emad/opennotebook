//! A draft's sources as a mind map: one model call, an outline parsed into a
//! tree, and every node checked against the sources before it is kept.
//!
//! The design and the evidence behind it are in `docs/mindmap-spec.md`. In
//! short: NotebookLM asks its model for a `{name, children}` JSON tree and
//! repairs trailing commas on the way in, which says the model breaks JSON. The
//! small models this studio runs break it more often, so the model writes an
//! indented `- ` outline instead, and the tree is built here. The stored shape
//! is still NotebookLM's.
//!
//! NotebookLM enforces nothing about where a node came from. This module does:
//! a node whose words appear nowhere in the sources, and none of whose
//! descendants appear either, is dropped and counted.

use crate::budget;
use crate::error::ScriptError;
use crate::grounding;

/// One source, as the map reads it.
#[derive(Debug, Clone)]
pub struct NamedDoc {
    /// The stored file name, as `source_list` reports it.
    pub name: String,
    pub title: String,
    pub text: String,
}

/// One node of the tree. A leaf has no children.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct MindNode {
    pub name: String,
    pub children: Vec<MindNode>,
}

impl MindNode {
    fn leaf(name: &str) -> Self {
        Self {
            name: name.to_string(),
            children: Vec::new(),
        }
    }

    /// Every node, the root included.
    pub fn count(&self) -> usize {
        1 + self.children.iter().map(MindNode::count).sum::<usize>()
    }

    /// Levels below this node: 0 for a leaf.
    pub fn depth(&self) -> usize {
        self.children
            .iter()
            .map(|c| 1 + c.depth())
            .max()
            .unwrap_or(0)
    }

    /// The tree as the outline the model writes: two spaces a level, `- `
    /// before every label. Also the Markdown export.
    pub fn to_outline(&self) -> String {
        let mut out = String::new();
        self.write_outline(0, &mut out);
        out
    }

    fn write_outline(&self, level: usize, out: &mut String) {
        out.push_str(&"  ".repeat(level));
        out.push_str("- ");
        out.push_str(&self.name);
        out.push('\n');
        for c in &self.children {
            c.write_outline(level + 1, out);
        }
    }
}

/// What a generation produced, with what it had to do to get there.
#[derive(Debug, Clone)]
pub struct MindMap {
    pub root: MindNode,
    /// Nodes removed because the sources never mention them.
    pub dropped: u32,
    /// The sources were cut to excerpts to fit one call.
    pub excerpted: bool,
    /// The grounding check did not run, because the map is in another language
    /// than English and its labels cannot be matched word for word against the
    /// sources.
    pub unchecked: bool,
    pub model: String,
}

/// Levels below the root. NotebookLM's real maps go three or four deep.
pub const MAX_DEPTH: usize = 4;

/// Children kept per node. The prompt asks for 2 to 6; a model that writes a
/// list of fifteen would make a column taller than any screen.
pub const MAX_CHILDREN: usize = 8;

/// A map with fewer nodes than this is asked for once more, and the larger of
/// the two is kept. Measured: on a 5,495 byte source, 1 run in 3 came back with
/// 6 or 7 nodes where the others had 20. A source too small for 12 still gets
/// whichever map was larger, so this never refuses a map.
pub const MIN_NODES: usize = 12;

/// The longest label kept, in characters. Six words is about 40.
pub const LABEL_CHARS: usize = 48;

/// Above this much source text the sources are cut to excerpts. About 150k
/// tokens; the largest real draft is 189,870 bytes.
pub const WHOLE_TEXT_CHARS: usize = 600_000;

const STAGE: &str = "mind map";

/// Build a map of `sources`.
///
/// `root_hint` names the root when the model leaves it out, which happens: a
/// model asked for one root often writes the main topics at the top level
/// instead. `focus` is the person's own words, or nothing.
pub async fn generate(
    sources: &[NamedDoc],
    root_hint: &str,
    focus: Option<&str>,
) -> Result<MindMap, ScriptError> {
    let model = opennotebook_session::settings::mindmap_model().await;
    let language = opennotebook_session::settings::language().await;
    let rule = opennotebook_session::settings::language_rule(&language);

    let (material, excerpted) = material(sources, root_hint);
    let system = system_prompt(focus, &rule);
    let user = user_prompt(&material, focus);
    let haystack = sources
        .iter()
        .map(|s| s.text.to_lowercase())
        .collect::<Vec<_>>()
        .join("\n");
    // Labels in another language cannot be matched against the sources' words,
    // and every one of them would be dropped. The rule is empty for English.
    let check = rule.is_empty();

    let mut best: Option<(MindNode, u32)> = None;
    for attempt in 0..2 {
        let user = if attempt == 0 {
            user.clone()
        } else {
            format!(
                "{user}\n\nYour last map was too thin: its lines were not indented under each \
                 other, or it had only a handful of topics. Write the root as the first line with \
                 no indentation, every main topic two spaces in, and every subtopic four spaces \
                 in, with 3 to 6 main topics and their subtopics."
            )
        };
        // A reply cut at the token ceiling is still a tree up to the cut, and
        // a map missing its last branch beats no map.
        let raw = match crate::generate::send(&model, &system, &user, STAGE, true).await {
            Ok(t) => t,
            Err(ScriptError::Truncated { text, .. }) if !text.trim().is_empty() => text,
            Err(e) => return Err(e),
        };
        let Some(tree) = parse_outline(&raw, root_hint) else {
            continue;
        };
        let (tree, dropped) = clean(tree, check.then_some(haystack.as_str()));
        let enough = !thin(&tree);
        if !enough {
            // A thin map is the one failure the model has shown here; its raw
            // reply is what says whether the model or the parser lost the rest.
            let head: String = raw.chars().take(800).collect();
            eprintln!(
                "opennotebook mindmap: attempt {} came back thin ({} nodes); reply starts:\n{head}",
                attempt + 1,
                tree.count()
            );
        }
        if best.as_ref().is_none_or(|(b, _)| tree.count() > b.count()) {
            best = Some((tree, dropped));
        }
        if enough {
            break;
        }
    }

    match best {
        Some((root, dropped)) if !root.children.is_empty() => Ok(MindMap {
            root,
            dropped,
            excerpted,
            unchecked: !check,
            model,
        }),
        _ => Err(ScriptError::Empty { stage: STAGE }),
    }
}

/// Too little to be worth showing without asking once more.
fn thin(tree: &MindNode) -> bool {
    tree.children.len() < 2 || tree.count() < MIN_NODES
}

/// The sources as one block of text, cut to excerpts when they are too long to
/// send whole. The second value says whether they were cut.
fn material(sources: &[NamedDoc], query: &str) -> (String, bool) {
    let total: usize = sources.iter().map(|s| s.text.chars().count()).sum();
    let cut = total > WHOLE_TEXT_CHARS;
    // An even share each, in pieces: a long source should not crowd out a
    // short one that is just as much a part of the draft.
    let keep = (WHOLE_TEXT_CHARS / sources.len().max(1) / grounding::EXCERPT_CHARS).max(1);
    let mut out = String::new();
    for (i, s) in sources.iter().enumerate() {
        let text = if cut {
            grounding::excerpts(std::slice::from_ref(&s.text), query, keep).join("\n\n")
        } else {
            s.text.clone()
        };
        out.push_str(&format!(
            "SOURCE {}: {}\n\n{}\n\n",
            i + 1,
            s.title.trim(),
            text.trim()
        ));
    }
    (out, cut)
}

fn system_prompt(focus: Option<&str>, language_rule: &str) -> String {
    let focus_rule = if focus.is_some_and(|f| !f.trim().is_empty()) {
        "\n- A focus is given below the material. Build the map around it and leave out \
         what does not bear on it."
    } else {
        ""
    };
    let mut s = format!(
        "You make a mind map of source material: the topics it covers and how they break \
         down, so a reader sees the whole of it at a glance.\n\
         Write it as an indented outline, two spaces per level, every line starting with \
         \"- \". The indentation is the map: a reply with every line at the same level has no \
         structure and is useless. Exactly this shape:\n\
         - Subject of the material\n\
         \u{20}\u{20}- Main topic\n\
         \u{20}\u{20}\u{20}\u{20}- Subtopic\n\
         Nothing else: no introduction, no headings, no commentary.\n\
         Rules:\n\
         - The first line is the root: the subject of the material, in at most 8 words. It \
           is the only line with no indentation; every other line is indented under it.\n\
         - Under the root, 3 to 6 main topics. Under each, 2 to 5 subtopics. At most \
           {MAX_DEPTH} levels below the root.\n\
         - Every label is a short noun phrase of at most 6 words, using the material's own \
           terms. No sentences, no numbering, no trailing punctuation, no explanations after \
           a colon.\n\
         - No two labels under the same parent say the same thing.\n\
         - Cover only what the material says. Do not add topics from your own knowledge.{focus_rule}"
    );
    if !language_rule.is_empty() {
        s.push_str(&format!(
            "\n\n{language_rule} Keep the \"- \" bullets and the indentation exactly as described."
        ));
    }
    s
}

fn user_prompt(material: &str, focus: Option<&str>) -> String {
    match focus.map(str::trim).filter(|f| !f.is_empty()) {
        Some(f) => format!("Material:\n\n{material}Focus: {f}"),
        None => format!("Material:\n\n{material}"),
    }
}

// ── parsing ─────────────────────────────────────────────────────────────────

/// Read a model's outline into a tree.
///
/// Indentation is read relative, not in fixed steps: a line belongs under the
/// nearest line above it that is indented less. So two spaces, four, tabs and a
/// mix of them all give the same tree, which is what a model's output needs.
///
/// The root is the one top-level line when there is one. When the model wrote
/// several at the top (the main topics, with the root left out), they go under
/// a root named by the last plain line before the outline, or else `root_hint`.
/// A plain line is any line that is not a bullet: "Here is the map:" before the
/// outline is one, and so is `# Title`.
///
/// `None` when there is no bullet at all.
pub fn parse_outline(raw: &str, root_hint: &str) -> Option<MindNode> {
    // (indent, label) for every bullet, in order.
    let mut items: Vec<(usize, String)> = Vec::new();
    let mut heading: Option<String> = None;
    for line in raw.lines() {
        let trimmed = line.trim();
        // A model that wraps its outline in a code fence: the fence lines are
        // not part of it, what is between them is.
        if trimmed.starts_with("```") {
            continue;
        }
        if trimmed.is_empty() {
            continue;
        }
        let indent: usize = line
            .chars()
            .take_while(|c| c.is_whitespace())
            .map(|c| if c == '\t' { 4 } else { 1 })
            .sum();
        match strip_bullet(trimmed) {
            Some(rest) => {
                let label = clean_label(rest);
                if !label.is_empty() {
                    items.push((indent, label));
                }
            }
            // A plain line names the root only while no bullet has been seen:
            // a line after the outline starts is commentary.
            None if items.is_empty() => {
                let label = clean_label(trimmed);
                if !label.is_empty() {
                    heading = Some(label);
                }
            }
            None => {}
        }
    }
    if items.is_empty() {
        return None;
    }

    // Build with a stack of (indent, path of child indexes from a virtual top).
    let mut top: Vec<MindNode> = Vec::new();
    let mut stack: Vec<(usize, Vec<usize>)> = Vec::new();
    for (indent, label) in items {
        while stack.last().is_some_and(|(i, _)| *i >= indent) {
            stack.pop();
        }
        let path = match stack.last() {
            None => {
                top.push(MindNode::leaf(&label));
                vec![top.len() - 1]
            }
            Some((_, parent)) => {
                let parent_node = node_at(&mut top, parent);
                parent_node.children.push(MindNode::leaf(&label));
                let mut p = parent.clone();
                p.push(parent_node.children.len() - 1);
                p
            }
        };
        stack.push((indent, path));
    }

    if top.len() == 1 {
        return top.pop();
    }
    let name = heading
        .filter(|h| !looks_like_preamble(h))
        .unwrap_or_else(|| clean_label(root_hint));
    Some(MindNode {
        name: if name.is_empty() {
            "Sources".to_string()
        } else {
            name
        },
        children: top,
    })
}

fn node_at<'a>(top: &'a mut [MindNode], path: &[usize]) -> &'a mut MindNode {
    let mut node = &mut top[path[0]];
    for &i in &path[1..] {
        node = &mut node.children[i];
    }
    node
}

/// "Here is a mind map of the material" is a line about the answer, not a
/// title of the material.
fn looks_like_preamble(line: &str) -> bool {
    let l = line.to_lowercase();
    ["here is", "here's", "mind map", "below is", "outline of"]
        .iter()
        .any(|p| l.contains(p))
}

/// The label after a bullet or a list number, or `None` for a line that is not
/// a list item.
fn strip_bullet(line: &str) -> Option<&str> {
    for b in ["- ", "* ", "• ", "– ", "+ "] {
        if let Some(rest) = line.strip_prefix(b) {
            return Some(rest);
        }
    }
    // "1. Label" and "1) Label".
    let digits = line.chars().take_while(char::is_ascii_digit).count();
    if digits > 0 {
        let rest = &line[digits..];
        if let Some(r) = rest.strip_prefix(". ").or_else(|| rest.strip_prefix(") ")) {
            return Some(r);
        }
    }
    None
}

/// A label without the decoration a model puts round it: emphasis, heading
/// marks, a trailing colon or full stop, and an explanation after a colon.
fn clean_label(raw: &str) -> String {
    let mut s = raw.trim().trim_start_matches('#').trim().to_string();
    for mark in ["**", "__", "`"] {
        s = s.replace(mark, "");
    }
    // "Cost: what the user pays per month" keeps "Cost". A label that is only
    // the part before the colon is never empty, because of the check.
    if let Some((head, tail)) = s.split_once(": ")
        && !head.trim().is_empty()
        && !tail.trim().is_empty()
    {
        s = head.to_string();
    }
    let s = s
        .trim()
        .trim_start_matches(['*', '_'])
        .trim_end_matches(['*', '_', ':', '.', ';', ',', ' '])
        .trim();
    budget::fit(s, LABEL_CHARS)
}

// ── cleaning ────────────────────────────────────────────────────────────────

/// The tree made safe to draw, and how many nodes the grounding check removed.
///
/// In order: siblings that say the same thing are merged, anything below
/// [`MAX_DEPTH`] is cut, each node keeps at most [`MAX_CHILDREN`], and, when
/// `sources` is given, a node is dropped when neither it nor anything under it
/// is mentioned there. The root is never dropped.
pub fn clean(mut root: MindNode, sources: Option<&str>) -> (MindNode, u32) {
    merge_siblings(&mut root);
    cut_depth(&mut root, 0);
    let mut dropped = 0u32;
    if let Some(text) = sources {
        root.children = root
            .children
            .into_iter()
            .filter_map(|c| keep_grounded(c, text, &mut dropped))
            .collect();
    }
    cap_children(&mut root);
    (root, dropped)
}

fn sibling_key(name: &str) -> String {
    name.chars()
        .filter(|c| c.is_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

fn merge_siblings(node: &mut MindNode) {
    let mut kept: Vec<MindNode> = Vec::new();
    for child in std::mem::take(&mut node.children) {
        let key = sibling_key(&child.name);
        match kept.iter_mut().find(|k| sibling_key(&k.name) == key) {
            Some(first) => first.children.extend(child.children),
            None => kept.push(child),
        }
    }
    node.children = kept;
    for c in &mut node.children {
        merge_siblings(c);
    }
}

fn cut_depth(node: &mut MindNode, level: usize) {
    if level >= MAX_DEPTH {
        node.children.clear();
        return;
    }
    for c in &mut node.children {
        cut_depth(c, level + 1);
    }
}

fn cap_children(node: &mut MindNode) {
    node.children.truncate(MAX_CHILDREN);
    for c in &mut node.children {
        cap_children(c);
    }
}

/// The node with its ungrounded descendants removed, or `None` when it and all
/// of them are ungrounded. An unmentioned label that holds mentioned ones is
/// kept: "Key ideas" is an organising heading, not an invention.
fn keep_grounded(mut node: MindNode, sources: &str, dropped: &mut u32) -> Option<MindNode> {
    node.children = std::mem::take(&mut node.children)
        .into_iter()
        .filter_map(|c| keep_grounded(c, sources, dropped))
        .collect();
    if node.children.is_empty() && !mentioned(&node.name, sources) {
        *dropped += 1;
        return None;
    }
    Some(node)
}

/// Whether any word of a label is in the sources. A label with no word worth
/// matching (all short or common) counts as mentioned: there is nothing to
/// check it by.
///
/// A plural or a verb form is matched by its stem as well, so "Costs" is found
/// in a text that says "cost".
fn mentioned(label: &str, sources: &str) -> bool {
    let terms = grounding::terms(label);
    terms.is_empty()
        || terms
            .iter()
            .any(|t| stems(t).iter().any(|s| sources.contains(s.as_str())))
}

fn stems(term: &str) -> Vec<String> {
    let mut out = vec![term.to_string()];
    for suffix in ["ies", "es", "s", "ing", "ed"] {
        if let Some(stem) = term.strip_suffix(suffix)
            && stem.chars().count() >= 3
        {
            out.push(stem.to_string());
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn names(n: &MindNode) -> Vec<&str> {
        n.children.iter().map(|c| c.name.as_str()).collect()
    }

    const TWO: &str =
        "- Transformers\n  - Attention\n    - Queries and keys\n  - Training\n    - Data\n";

    #[test]
    fn indentation_by_two_four_tabs_or_a_mix_gives_one_tree() {
        let two = parse_outline(TWO, "x").unwrap();
        let four = "- Transformers\n    - Attention\n        - Queries and keys\n    - Training\n        - Data\n";
        let tabs =
            "- Transformers\n\t- Attention\n\t\t- Queries and keys\n\t- Training\n\t\t- Data\n";
        let mixed =
            "- Transformers\n  - Attention\n      - Queries and keys\n  - Training\n    - Data\n";
        for other in [four, tabs, mixed] {
            assert_eq!(parse_outline(other, "x").unwrap(), two, "{other}");
        }
        assert_eq!(two.name, "Transformers");
        assert_eq!(names(&two), ["Attention", "Training"]);
        assert_eq!(two.depth(), 2);
    }

    #[test]
    fn decoration_is_stripped_from_labels() {
        for raw in [
            "- **Bold label**:",
            "1. Bold label",
            "* Bold label",
            "- Bold label.",
            "2) `Bold label`",
            "- ## Bold label",
        ] {
            let t = parse_outline(&format!("- Root\n  {raw}\n"), "x").unwrap();
            assert_eq!(names(&t), ["Bold label"], "{raw}");
        }
    }

    #[test]
    fn an_explanation_after_a_colon_is_dropped() {
        let t = parse_outline("- Root\n  - Cost: what the user pays each month\n", "x").unwrap();
        assert_eq!(names(&t), ["Cost"]);
    }

    #[test]
    fn a_long_label_is_cut_at_a_word() {
        let long = "word ".repeat(30);
        let t = parse_outline(&format!("- Root\n  - {long}\n"), "x").unwrap();
        assert!(t.children[0].name.chars().count() <= LABEL_CHARS);
        assert!(!t.children[0].name.ends_with(' '));
    }

    #[test]
    fn a_missing_root_is_taken_from_the_title_line_or_the_hint() {
        let titled = "# Rust ownership\n- Borrowing\n- Lifetimes\n";
        let t = parse_outline(titled, "hint").unwrap();
        assert_eq!(t.name, "Rust ownership");
        assert_eq!(names(&t), ["Borrowing", "Lifetimes"]);

        let preamble = "Here is a mind map of the material:\n- Borrowing\n- Lifetimes\n";
        assert_eq!(
            parse_outline(preamble, "My draft").unwrap().name,
            "My draft"
        );
    }

    #[test]
    fn commentary_after_the_outline_is_not_a_node() {
        let t = parse_outline("- Root\n  - A\n  - B\nI hope this helps.\n", "x").unwrap();
        assert_eq!(names(&t), ["A", "B"]);
    }

    #[test]
    fn a_fenced_reply_parses_like_a_bare_one() {
        let fenced = format!("```markdown\n{TWO}```\n");
        assert_eq!(parse_outline(&fenced, "x"), parse_outline(TWO, "x"));
    }

    #[test]
    fn no_bullet_is_no_map() {
        assert!(parse_outline("I cannot make a map of this.", "x").is_none());
    }

    #[test]
    fn siblings_saying_the_same_thing_merge_and_keep_both_childrens() {
        let t = parse_outline(
            "- Root\n  - Cost\n    - Hosting\n  - cost.\n    - Licences\n",
            "x",
        )
        .unwrap();
        let (t, _) = clean(t, None);
        assert_eq!(names(&t), ["Cost"]);
        assert_eq!(names(&t.children[0]), ["Hosting", "Licences"]);
    }

    #[test]
    fn nothing_is_kept_below_the_depth_limit() {
        let deep = "- R\n  - 1\n    - 2\n      - 3\n        - 4\n          - 5\n";
        let (t, _) = clean(parse_outline(deep, "x").unwrap(), None);
        assert_eq!(t.depth(), MAX_DEPTH);
        assert_eq!(t.count(), MAX_DEPTH + 1);
    }

    #[test]
    fn a_node_the_sources_never_mention_is_dropped_with_its_children() {
        let raw = "- Rust\n  - Ownership\n    - Borrow checker\n  - Quantum Entanglement\n    - Spooky action\n";
        let sources = "rust ownership is enforced by the borrow checker at compile time.";
        let (t, dropped) = clean(parse_outline(raw, "x").unwrap(), Some(sources));
        assert_eq!(names(&t), ["Ownership"]);
        assert_eq!(dropped, 2);
    }

    #[test]
    fn an_organising_label_over_mentioned_ones_is_kept() {
        let raw = "- Rust\n  - Key ideas\n    - Ownership\n    - Unicorns\n";
        let (t, dropped) = clean(parse_outline(raw, "x").unwrap(), Some("ownership rules"));
        assert_eq!(names(&t), ["Key ideas"]);
        assert_eq!(names(&t.children[0]), ["Ownership"]);
        assert_eq!(dropped, 1);
    }

    #[test]
    fn a_plural_label_is_found_by_its_stem() {
        assert!(mentioned("Costs", "the cost of hosting"));
        assert!(mentioned("Training runs", "we train for a week"));
        assert!(!mentioned("Unicorns", "the cost of hosting"));
        // Nothing worth matching: nothing to check it by.
        assert!(mentioned("Why", "anything"));
    }

    #[test]
    fn the_root_survives_even_when_unmentioned() {
        let (t, _) = clean(
            parse_outline("- Zebra\n  - Ownership\n", "x").unwrap(),
            Some("ownership"),
        );
        assert_eq!(t.name, "Zebra");
    }

    #[test]
    fn a_node_keeps_at_most_the_child_cap() {
        let mut raw = String::from("- Root\n");
        for i in 0..20 {
            raw.push_str(&format!("  - Topic {i}\n"));
        }
        let (t, _) = clean(parse_outline(&raw, "x").unwrap(), None);
        assert_eq!(t.children.len(), MAX_CHILDREN);
    }

    #[test]
    fn a_map_of_a_handful_of_nodes_is_thin() {
        let few = parse_outline("- R\n  - A\n    - a1\n  - B\n    - b1\n  - C\n", "x").unwrap();
        assert!(thin(&few));
        let mut raw = String::from("- R\n");
        for t in ["A", "B", "C", "D"] {
            raw.push_str(&format!("  - {t}\n    - {t}1\n    - {t}2\n"));
        }
        assert!(!thin(&parse_outline(&raw, "x").unwrap()));
        assert!(thin(&parse_outline("- R\n  - Only\n", "x").unwrap()));
    }

    #[test]
    fn the_outline_export_reads_back_as_the_same_tree() {
        let t = parse_outline(TWO, "x").unwrap();
        assert_eq!(parse_outline(&t.to_outline(), "x").unwrap(), t);
    }

    #[test]
    fn sources_under_the_limit_go_whole_and_over_it_go_as_excerpts() {
        let small = vec![NamedDoc {
            name: "a.md".into(),
            title: "A".into(),
            text: "Short text.".into(),
        }];
        let (m, cut) = material(&small, "q");
        assert!(!cut);
        assert!(m.contains("SOURCE 1: A") && m.contains("Short text."));

        let para = "Ownership moves values between bindings in a program.\n\n";
        let big = vec![NamedDoc {
            name: "b.md".into(),
            title: "B".into(),
            text: para.repeat(WHOLE_TEXT_CHARS / para.len() + 100),
        }];
        let (m, cut) = material(&big, "ownership");
        assert!(cut);
        assert!(m.chars().count() <= WHOLE_TEXT_CHARS + 1_000);
    }

    /// The example in the prompt must itself parse to a nested tree: a model
    /// copies its shape, so a broken example teaches the flat reply.
    #[test]
    fn the_prompts_example_is_a_nested_outline() {
        let p = system_prompt(None, "");
        let start = p.find("- Subject").unwrap();
        let example: String = p[start..].lines().take(3).collect::<Vec<_>>().join("\n");
        let t = parse_outline(&example, "x").unwrap();
        assert_eq!(t.depth(), 2, "{example}");
    }

    #[test]
    fn the_focus_reaches_both_prompts_only_when_given() {
        assert!(system_prompt(Some("security"), "").contains("A focus is given"));
        assert!(!system_prompt(None, "").contains("A focus is given"));
        assert!(!system_prompt(Some("  "), "").contains("A focus is given"));
        assert!(user_prompt("M\n\n", Some("security")).ends_with("Focus: security"));
        assert!(!user_prompt("M\n\n", None).contains("Focus"));
    }
}
