//! Where every node of a mind map goes, and the few other things about a map
//! that are pure functions: the question a click asks, and the exports.
//!
//! It lives in the SDK for the reason `styles` does: it is the one crate the
//! wasm app can depend on that also builds natively, so this is tested with
//! `cargo test` and not in a browser. The app draws what this returns as SVG.
//!
//! The geometry is NotebookLM's, read from its shipped viewer (see
//! `docs/mindmap-spec.md` section 1): a tree growing left to right, each depth
//! a column as wide as its widest label plus a 60px gap, leaves on consecutive
//! rows, each parent centred on its children, and curved links from a parent's
//! right edge to a child's left edge.

use std::collections::HashSet;

use crate::mindmap::MindNode;

/// A node's address: the child indexes from the root. The root is `[]`. A
/// path survives a re-render, where a reference would not.
pub type NodePath = Vec<usize>;

/// Distance between the centres of two neighbouring rows.
pub const ROW: f32 = 44.0;
/// A node box's height.
pub const NODE_H: f32 = 32.0;
/// Space either side of a label inside its box.
pub const PAD_X: f32 = 14.0;
/// Room on a branch's right edge for its open/close circle.
pub const TOGGLE_W: f32 = 22.0;
/// Space between one column and the next.
pub const GAP: f32 = 60.0;
/// Label size the app draws with, in px.
pub const FONT_PX: f32 = 15.0;

/// Fill and text colour per depth, as CSS variables from `theme.css`, so the
/// map follows the theme. Deeper than 4 uses the last. Their contrast is held
/// to WCAG AA in both themes by `theme`'s tests.
///
/// They are CSS, not SVG attributes: `fill="var(..)"` is not valid, so the app
/// sets them through `style`. The PNG export resolves them to real colours.
pub const DEPTH_COLOURS: [(&str, &str); 5] = [
    ("var(--st-mm-d0)", "var(--st-mm-t0)"),
    ("var(--st-mm-d1)", "var(--st-mm-t1)"),
    ("var(--st-mm-d2)", "var(--st-mm-t2)"),
    ("var(--st-mm-d3)", "var(--st-mm-t3)"),
    ("var(--st-mm-d4)", "var(--st-mm-t4)"),
];

/// The links between nodes.
pub const LINK_COLOUR: &str = "var(--st-mm-link)";

/// (fill, text) for a node at `depth`.
pub fn colours(depth: usize) -> (&'static str, &'static str) {
    DEPTH_COLOURS[depth.min(DEPTH_COLOURS.len() - 1)]
}

/// One drawn node.
#[derive(Debug, Clone, PartialEq)]
pub struct Placed {
    pub path: NodePath,
    pub name: String,
    /// 0 for the root.
    pub depth: usize,
    /// Top left corner.
    pub x: f32,
    pub y: f32,
    pub w: f32,
    pub h: f32,
    /// How many children it has, drawn or not. 0 is a leaf.
    pub children: usize,
    /// Its children are drawn.
    pub open: bool,
}

impl Placed {
    pub fn cy(&self) -> f32 {
        self.y + self.h / 2.0
    }
}

/// One drawn link, from a parent to a child.
#[derive(Debug, Clone, PartialEq)]
pub struct Link {
    pub to: NodePath,
    pub x1: f32,
    pub y1: f32,
    pub x2: f32,
    pub y2: f32,
}

impl Link {
    /// The SVG path: a cubic curve leaving the parent and arriving at the
    /// child horizontally, bending halfway across the gap.
    pub fn d(&self) -> String {
        let mx = (self.x1 + self.x2) / 2.0;
        format!(
            "M{:.1},{:.1} C{:.1},{:.1} {:.1},{:.1} {:.1},{:.1}",
            self.x1, self.y1, mx, self.y1, mx, self.y2, self.x2, self.y2
        )
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Layout {
    /// Drawn nodes in reading order: depth first, which is also the order the
    /// keyboard's Up and Down move through.
    pub nodes: Vec<Placed>,
    pub links: Vec<Link>,
    pub width: f32,
    pub height: f32,
}

impl Layout {
    pub fn find(&self, path: &[usize]) -> Option<&Placed> {
        self.nodes.iter().find(|n| n.path == path)
    }

    /// The box around a node and its drawn children, for fitting the view to
    /// it after a click: (x, y, width, height).
    pub fn bounds_of(&self, path: &[usize]) -> Option<(f32, f32, f32, f32)> {
        let mut it = self.nodes.iter().filter(|n| {
            n.path == path || (n.path.len() == path.len() + 1 && n.path.starts_with(path))
        });
        let first = it.next()?;
        let (mut x0, mut y0, mut x1, mut y1) =
            (first.x, first.y, first.x + first.w, first.y + first.h);
        for n in it {
            x0 = x0.min(n.x);
            y0 = y0.min(n.y);
            x1 = x1.max(n.x + n.w);
            y1 = y1.max(n.y + n.h);
        }
        Some((x0, y0, x1 - x0, y1 - y0))
    }
}

/// A label's width in px when the browser cannot measure it: 0.55 of the font
/// size per character, close to Inter and the system sans at 15px.
pub fn estimate_width(label: &str) -> f32 {
    label.chars().count() as f32 * FONT_PX * 0.55
}

/// Lay out the drawn part of `root`. A node's children are drawn when its path
/// is in `open`; the root is always drawn. `measure` gives a label's width in
/// px; [`estimate_width`] is the fallback.
pub fn layout(root: &MindNode, open: &HashSet<NodePath>, measure: &dyn Fn(&str) -> f32) -> Layout {
    // Pass 1: what is drawn, depth first, with each box's width.
    struct Item {
        path: NodePath,
        name: String,
        w: f32,
        children: usize,
        open: bool,
    }
    fn walk(
        n: &MindNode,
        path: NodePath,
        open: &HashSet<NodePath>,
        measure: &dyn Fn(&str) -> f32,
        out: &mut Vec<Item>,
    ) {
        let is_open = !n.children.is_empty() && open.contains(&path);
        let toggle = if n.children.is_empty() { 0.0 } else { TOGGLE_W };
        out.push(Item {
            path: path.clone(),
            name: n.name.clone(),
            w: measure(&n.name) + 2.0 * PAD_X + toggle,
            children: n.children.len(),
            open: is_open,
        });
        if is_open {
            for (i, c) in n.children.iter().enumerate() {
                let mut p = path.clone();
                p.push(i);
                walk(c, p, open, measure, out);
            }
        }
    }
    let mut items = Vec::new();
    walk(root, Vec::new(), open, measure, &mut items);

    // Columns: each as wide as its widest drawn box.
    let depth_max = items.iter().map(|i| i.path.len()).max().unwrap_or(0);
    let mut col_w = vec![0f32; depth_max + 1];
    for i in &items {
        col_w[i.path.len()] = col_w[i.path.len()].max(i.w);
    }
    let mut col_x = vec![0f32; depth_max + 1];
    for d in 1..=depth_max {
        col_x[d] = col_x[d - 1] + col_w[d - 1] + GAP;
    }

    // Pass 2: rows. Drawn leaves take consecutive rows in reading order; a
    // parent's centre is the midpoint of its first and last drawn child's.
    let mut cy = vec![0f32; items.len()];
    let mut row = 0usize;
    // Items are depth first, so a node's drawn descendants are the run of
    // items after it with a longer path.
    fn place(idx: usize, items: &[Item], cy: &mut [f32], row: &mut usize) -> usize {
        // Returns the index just past this node's subtree.
        let depth = items[idx].path.len();
        let mut next = idx + 1;
        let mut first: Option<f32> = None;
        let mut last = 0f32;
        while next < items.len() && items[next].path.len() > depth {
            let child = next;
            next = place(child, items, cy, row);
            first.get_or_insert(cy[child]);
            last = cy[child];
        }
        cy[idx] = match first {
            Some(f) => (f + last) / 2.0,
            None => {
                let y = *row as f32 * ROW + ROW / 2.0;
                *row += 1;
                y
            }
        };
        next
    }
    if !items.is_empty() {
        place(0, &items, &mut cy, &mut row);
    }

    let nodes: Vec<Placed> = items
        .into_iter()
        .zip(cy)
        .map(|(i, c)| Placed {
            depth: i.path.len(),
            x: col_x[i.path.len()],
            y: c - NODE_H / 2.0,
            w: i.w,
            h: NODE_H,
            children: i.children,
            open: i.open,
            path: i.path,
            name: i.name,
        })
        .collect();

    let mut links = Vec::new();
    for n in &nodes {
        if n.path.is_empty() {
            continue;
        }
        let parent = &n.path[..n.path.len() - 1];
        if let Some(p) = nodes.iter().find(|p| p.path == parent) {
            links.push(Link {
                to: n.path.clone(),
                x1: p.x + p.w,
                y1: p.cy(),
                x2: n.x,
                y2: n.cy(),
            });
        }
    }

    let width = nodes.iter().map(|n| n.x + n.w).fold(0.0, f32::max);
    Layout {
        nodes,
        links,
        width,
        height: row as f32 * ROW,
    }
}

/// What NotebookLM opens with: the root's children drawn, nothing deeper.
pub fn initial_open() -> HashSet<NodePath> {
    HashSet::from([Vec::new()])
}

/// Every branch open.
pub fn all_open(root: &MindNode) -> HashSet<NodePath> {
    fn walk(n: &MindNode, path: NodePath, out: &mut HashSet<NodePath>) {
        if n.children.is_empty() {
            return;
        }
        for (i, c) in n.children.iter().enumerate() {
            let mut p = path.clone();
            p.push(i);
            walk(c, p, out);
        }
        out.insert(path);
    }
    let mut out = HashSet::new();
    walk(root, Vec::new(), &mut out);
    out
}

/// The node at `path`, if there is one.
pub fn node_at<'a>(root: &'a MindNode, path: &[usize]) -> Option<&'a MindNode> {
    let mut n = root;
    for &i in path {
        n = n.children.get(i)?;
    }
    Some(n)
}

/// The question a click on a node asks the chat. NotebookLM's own sentence,
/// word for word, from its shipped viewer: the node, and its direct parent
/// only, never the path above that. The root has no parent clause.
pub fn question_for(root: &MindNode, path: &[usize]) -> Option<String> {
    let node = node_at(root, path)?;
    Some(match path.split_last() {
        None => format!("Discuss what these sources say about {}.", node.name),
        Some((_, parent)) => format!(
            "Discuss what these sources say about {}, in the larger context of {}.",
            node.name,
            node_at(root, parent)?.name
        ),
    })
}

/// The map as an indented `- ` outline, the same format the model writes it
/// in. The Markdown export.
pub fn to_markdown(root: &MindNode) -> String {
    fn walk(n: &MindNode, level: usize, out: &mut String) {
        out.push_str(&"  ".repeat(level));
        out.push_str("- ");
        out.push_str(&n.name);
        out.push('\n');
        for c in &n.children {
            walk(c, level + 1, out);
        }
    }
    let mut out = String::new();
    walk(root, 0, &mut out);
    out
}

/// The map as OPML 2.0, which mind map and outliner apps open.
pub fn to_opml(root: &MindNode) -> String {
    fn esc(s: &str) -> String {
        s.replace('&', "&amp;")
            .replace('<', "&lt;")
            .replace('>', "&gt;")
            .replace('"', "&quot;")
    }
    fn walk(n: &MindNode, level: usize, out: &mut String) {
        let pad = "  ".repeat(level + 2);
        if n.children.is_empty() {
            out.push_str(&format!("{pad}<outline text=\"{}\"/>\n", esc(&n.name)));
        } else {
            out.push_str(&format!("{pad}<outline text=\"{}\">\n", esc(&n.name)));
            for c in &n.children {
                walk(c, level + 1, out);
            }
            out.push_str(&format!("{pad}</outline>\n"));
        }
    }
    let mut out = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<opml version=\"2.0\">\n  <head><title>{}</title></head>\n  <body>\n",
        esc(&root.name)
    );
    walk(root, 0, &mut out);
    out.push_str("  </body>\n</opml>\n");
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn n(name: &str, children: Vec<MindNode>) -> MindNode {
        MindNode {
            name: name.into(),
            children,
        }
    }

    fn leaf(name: &str) -> MindNode {
        n(name, vec![])
    }

    /// Root, three branches, uneven depth, one long label.
    fn sample() -> MindNode {
        n(
            "Moshi",
            vec![
                n(
                    "Real-time dialogue framework",
                    vec![
                        n(
                            "Inner Monologue method",
                            vec![leaf("Text tokens"), leaf("Audio tokens")],
                        ),
                        leaf("Full-duplex dialogue"),
                    ],
                ),
                n(
                    "Latency",
                    vec![leaf("Theoretical 160ms"), leaf("Practical 200ms")],
                ),
                leaf("Limitations of current systems and why pipelines of components fail"),
            ],
        )
    }

    fn overlaps(a: &Placed, b: &Placed) -> bool {
        a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h
    }

    fn check_invariants(l: &Layout) {
        for (i, a) in l.nodes.iter().enumerate() {
            for b in &l.nodes[i + 1..] {
                assert!(!overlaps(a, b), "{} overlaps {}", a.name, b.name);
            }
        }
        for c in &l.nodes {
            let Some((_, parent)) = c.path.split_last() else {
                continue;
            };
            let p = l.find(parent).expect("a drawn node's parent is drawn");
            assert!(
                c.x >= p.x + p.w + GAP - 0.01,
                "{} is not right of {}",
                c.name,
                p.name
            );
        }
        for p in l.nodes.iter().filter(|p| p.open) {
            let kids: Vec<&Placed> = l
                .nodes
                .iter()
                .filter(|c| c.path.len() == p.path.len() + 1 && c.path.starts_with(&p.path))
                .collect();
            let (lo, hi) = (kids.first().unwrap().cy(), kids.last().unwrap().cy());
            assert!(
                p.cy() >= lo - 0.01 && p.cy() <= hi + 0.01,
                "{} is not between its children",
                p.name
            );
        }
    }

    #[test]
    fn it_opens_like_notebooklm_root_and_its_children_only() {
        let l = layout(&sample(), &initial_open(), &estimate_width);
        let names: Vec<&str> = l.nodes.iter().map(|n| n.name.as_str()).collect();
        assert_eq!(names[0], "Moshi");
        assert_eq!(l.nodes.len(), 4);
        assert!(l.nodes.iter().skip(1).all(|n| n.depth == 1 && !n.open));
        assert_eq!(l.links.len(), 3);
        check_invariants(&l);
    }

    #[test]
    fn fully_open_nothing_overlaps_and_every_child_is_right_of_its_parent() {
        let t = sample();
        let l = layout(&t, &all_open(&t), &estimate_width);
        assert_eq!(l.nodes.len(), 10);
        assert_eq!(l.links.len(), 9);
        check_invariants(&l);
        // Six drawn leaves, six rows.
        assert_eq!(l.height, 6.0 * ROW);
    }

    #[test]
    fn a_column_is_as_wide_as_its_widest_label() {
        let t = sample();
        let l = layout(&t, &initial_open(), &estimate_width);
        let widest = l
            .nodes
            .iter()
            .filter(|n| n.depth == 1)
            .map(|n| n.w)
            .fold(0.0, f32::max);
        let long = l.find(&[2]).unwrap();
        assert_eq!(long.w, widest);
        assert_eq!(l.width, long.x + widest);
    }

    #[test]
    fn closing_a_node_hides_exactly_its_descendants_and_moves_nothing_above_it() {
        let t = sample();
        let mut open = all_open(&t);
        let before = layout(&t, &open, &estimate_width);
        open.remove(&vec![0, 0]);
        let after = layout(&t, &open, &estimate_width);

        let gone: Vec<&str> = before
            .nodes
            .iter()
            .filter(|b| after.find(&b.path).is_none())
            .map(|b| b.name.as_str())
            .collect();
        assert_eq!(gone, ["Text tokens", "Audio tokens"]);

        // Above it in reading order, and not one of its ancestors: unmoved.
        let at = before.nodes.iter().position(|n| n.path == [0, 0]).unwrap();
        for b in &before.nodes[..at] {
            if [0, 0].starts_with(&b.path) {
                continue;
            }
            assert_eq!(after.find(&b.path).unwrap().y, b.y, "{} moved", b.name);
        }
        check_invariants(&after);
    }

    #[test]
    fn a_leaf_has_no_toggle_room_and_a_branch_has() {
        let t = n("R", vec![leaf("Same"), n("Same", vec![leaf("x")])]);
        let l = layout(&t, &initial_open(), &estimate_width);
        assert_eq!(l.find(&[1]).unwrap().w - l.find(&[0]).unwrap().w, TOGGLE_W);
    }

    #[test]
    fn the_click_question_is_notebooklms_sentence() {
        let t = sample();
        assert_eq!(
            question_for(&t, &[]).unwrap(),
            "Discuss what these sources say about Moshi."
        );
        assert_eq!(
            question_for(&t, &[0]).unwrap(),
            "Discuss what these sources say about Real-time dialogue framework, in the larger context of Moshi."
        );
        // The direct parent only, never the grandparent.
        let deep = question_for(&t, &[0, 0, 1]).unwrap();
        assert_eq!(
            deep,
            "Discuss what these sources say about Audio tokens, in the larger context of Inner Monologue method."
        );
        assert!(!deep.contains("Moshi"));
        assert!(question_for(&t, &[9]).is_none());
    }

    #[test]
    fn a_click_fits_the_node_and_its_children() {
        let t = sample();
        let l = layout(&t, &all_open(&t), &estimate_width);
        let (x, y, w, h) = l.bounds_of(&[1]).unwrap();
        let p = l.find(&[1]).unwrap();
        assert_eq!(x, p.x);
        for c in [l.find(&[1, 0]).unwrap(), l.find(&[1, 1]).unwrap()] {
            assert!(c.y >= y && c.y + c.h <= y + h + 0.01);
            assert!(c.x + c.w <= x + w + 0.01);
        }
    }

    #[test]
    fn exports_carry_every_node() {
        let t = sample();
        let md = to_markdown(&t);
        assert_eq!(md.lines().count(), 10);
        assert!(md.contains("\n    - Inner Monologue method\n"));
        let t2 = n("A & <B>", vec![leaf("\"q\"")]);
        let opml = to_opml(&t2);
        assert!(opml.contains("<title>A &amp; &lt;B&gt;</title>"));
        assert!(opml.contains("<outline text=\"&quot;q&quot;\"/>"));
        assert_eq!(to_opml(&t).matches("<outline").count(), 10);
    }

    /// Every colour the map uses is a token the theme defines, in both
    /// themes; `theme`'s tests hold those tokens' contrast.
    #[test]
    fn every_map_colour_is_a_theme_token() {
        let css = crate::theme::CSS;
        let mut names: Vec<&str> = DEPTH_COLOURS.iter().flat_map(|(f, t)| [*f, *t]).collect();
        names.push(LINK_COLOUR);
        for v in names {
            let name = v.trim_start_matches("var(").trim_end_matches(')');
            assert_eq!(
                css.matches(&format!("{name}:")).count(),
                2,
                "{name} is not set in both themes"
            );
        }
        assert_eq!(colours(9), colours(4));
    }

    #[test]
    fn a_link_bends_halfway_across_the_gap() {
        let l = Link {
            to: vec![0],
            x1: 0.0,
            y1: 10.0,
            x2: 100.0,
            y2: 50.0,
        };
        assert_eq!(l.d(), "M0.0,10.0 C50.0,10.0 50.0,50.0 100.0,50.0");
    }
}
