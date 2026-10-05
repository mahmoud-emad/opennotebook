// Where every node of a mind map goes, and the few other things about a map
// that are pure functions: the question a click asks, and the exports. A port
// of the old SDK's `mindmap_layout.rs`, tested the same way.
//
// The geometry is NotebookLM's, read from its shipped viewer (see
// `docs/mindmap-spec.md` section 1): a tree growing left to right, each depth
// a column as wide as its widest label plus a 60px gap, leaves on consecutive
// rows, each parent centred on its children, and curved links from a parent's
// right edge to a child's left edge.

/** One topic of a map and the topics under it. */
export type MindNode = { name: string; children?: MindNode[] };

/** A node's address: the child indexes from the root. The root is `[]`. A
 * path survives a re-render, where a reference would not. */
export type NodePath = number[];

/** The set of open branches, by `pathKey`. */
export type OpenSet = Set<string>;

/** A path as a key for an `OpenSet`: the root is "". */
export const pathKey = (p: NodePath) => p.join(",");

/** Distance between the centres of two neighbouring rows. */
export const ROW = 44;
/** A node box's height. */
export const NODE_H = 32;
/** Space either side of a label inside its box. */
export const PAD_X = 14;
/** Room on a branch's right edge for its open/close circle. */
export const TOGGLE_W = 22;
/** Space between one column and the next. */
export const GAP = 60;
/** Label size the app draws with, in px. */
export const FONT_PX = 15;

/** Fill and text colour per depth, as CSS variables from `theme.css`, so the
 * map follows the theme. Deeper than 4 uses the last.
 *
 * They are CSS, not SVG attributes: `fill="var(..)"` is not valid, so the app
 * sets them through `style`. The PNG export resolves them to real colours. */
export const DEPTH_COLOURS: [string, string][] = [
  ["var(--st-mm-d0)", "var(--st-mm-t0)"],
  ["var(--st-mm-d1)", "var(--st-mm-t1)"],
  ["var(--st-mm-d2)", "var(--st-mm-t2)"],
  ["var(--st-mm-d3)", "var(--st-mm-t3)"],
  ["var(--st-mm-d4)", "var(--st-mm-t4)"],
];

/** The links between nodes. */
export const LINK_COLOUR = "var(--st-mm-link)";

/** [fill, text] for a node at `depth`. */
export function colours(depth: number): [string, string] {
  return DEPTH_COLOURS[Math.min(depth, DEPTH_COLOURS.length - 1)]!;
}

/** One drawn node. */
export type Placed = {
  path: NodePath;
  name: string;
  /** 0 for the root. */
  depth: number;
  /** Top left corner. */
  x: number;
  y: number;
  w: number;
  h: number;
  /** How many children it has, drawn or not. 0 is a leaf. */
  children: number;
  /** Its children are drawn. */
  open: boolean;
};

export const cy = (n: Placed) => n.y + n.h / 2;

/** One drawn link, from a parent to a child. */
export type Link = { to: NodePath; x1: number; y1: number; x2: number; y2: number };

/** The SVG path: a cubic curve leaving the parent and arriving at the child
 * horizontally, bending halfway across the gap. */
export function linkD(l: Link): string {
  const mx = (l.x1 + l.x2) / 2;
  const f = (v: number) => v.toFixed(1);
  return `M${f(l.x1)},${f(l.y1)} C${f(mx)},${f(l.y1)} ${f(mx)},${f(l.y2)} ${f(l.x2)},${f(l.y2)}`;
}

export type Layout = {
  /** Drawn nodes in reading order: depth first, which is also the order the
   * keyboard's Up and Down move through. */
  nodes: Placed[];
  links: Link[];
  width: number;
  height: number;
};

export const EMPTY_LAYOUT: Layout = { nodes: [], links: [], width: 0, height: 0 };

const samePath = (a: NodePath, b: NodePath) => a.length === b.length && a.every((v, i) => v === b[i]);
const startsWith = (p: NodePath, pre: NodePath) => pre.every((v, i) => p[i] === v);

export function find(l: Layout, path: NodePath): Placed | undefined {
  return l.nodes.find((n) => samePath(n.path, path));
}

/** The box around a node and its drawn children, for fitting the view to it
 * after a click: [x, y, width, height]. */
export function boundsOf(l: Layout, path: NodePath): [number, number, number, number] | null {
  const it = l.nodes.filter(
    (n) => samePath(n.path, path) || (n.path.length === path.length + 1 && startsWith(n.path, path)),
  );
  const first = it[0];
  if (!first) return null;
  let [x0, y0, x1, y1] = [first.x, first.y, first.x + first.w, first.y + first.h];
  for (const n of it.slice(1)) {
    x0 = Math.min(x0, n.x);
    y0 = Math.min(y0, n.y);
    x1 = Math.max(x1, n.x + n.w);
    y1 = Math.max(y1, n.y + n.h);
  }
  return [x0, y0, x1 - x0, y1 - y0];
}

/** A label's width in px when the browser cannot measure it: 0.55 of the font
 * size per character, close to Inter and the system sans at 15px. */
export function estimateWidth(label: string): number {
  return [...label].length * FONT_PX * 0.55;
}

/** Lay out the drawn part of `root`. A node's children are drawn when its path
 * is in `open`; the root is always drawn. `measure` gives a label's width in
 * px; `estimateWidth` is the fallback. */
export function layout(root: MindNode, open: OpenSet, measure: (label: string) => number): Layout {
  // Pass 1: what is drawn, depth first, with each box's width.
  type Item = { path: NodePath; name: string; w: number; children: number; open: boolean };
  const items: Item[] = [];
  const walk = (n: MindNode, path: NodePath) => {
    const kids = n.children ?? [];
    const isOpen = kids.length > 0 && open.has(pathKey(path));
    const toggle = kids.length === 0 ? 0 : TOGGLE_W;
    items.push({ path, name: n.name, w: measure(n.name) + 2 * PAD_X + toggle, children: kids.length, open: isOpen });
    if (isOpen) kids.forEach((c, i) => walk(c, [...path, i]));
  };
  walk(root, []);

  // Columns: each as wide as its widest drawn box.
  const depthMax = Math.max(0, ...items.map((i) => i.path.length));
  const colW = new Array<number>(depthMax + 1).fill(0);
  for (const i of items) colW[i.path.length] = Math.max(colW[i.path.length]!, i.w);
  const colX = new Array<number>(depthMax + 1).fill(0);
  for (let d = 1; d <= depthMax; d++) colX[d] = colX[d - 1]! + colW[d - 1]! + GAP;

  // Pass 2: rows. Drawn leaves take consecutive rows in reading order; a
  // parent's centre is the midpoint of its first and last drawn child's.
  const cys = new Array<number>(items.length).fill(0);
  let row = 0;
  // Items are depth first, so a node's drawn descendants are the run of
  // items after it with a longer path. Returns the index just past this
  // node's subtree.
  const place = (idx: number): number => {
    const depth = items[idx]!.path.length;
    let next = idx + 1;
    let first: number | null = null;
    let last = 0;
    while (next < items.length && items[next]!.path.length > depth) {
      const child = next;
      next = place(child);
      if (first === null) first = cys[child]!;
      last = cys[child]!;
    }
    if (first !== null) cys[idx] = (first + last) / 2;
    else {
      cys[idx] = row * ROW + ROW / 2;
      row += 1;
    }
    return next;
  };
  if (items.length > 0) place(0);

  const nodes: Placed[] = items.map((i, k) => ({
    depth: i.path.length,
    x: colX[i.path.length]!,
    y: cys[k]! - NODE_H / 2,
    w: i.w,
    h: NODE_H,
    children: i.children,
    open: i.open,
    path: i.path,
    name: i.name,
  }));

  const links: Link[] = [];
  for (const n of nodes) {
    if (n.path.length === 0) continue;
    const parent = n.path.slice(0, -1);
    const p = nodes.find((p) => samePath(p.path, parent));
    if (p) links.push({ to: n.path, x1: p.x + p.w, y1: cy(p), x2: n.x, y2: cy(n) });
  }

  const width = nodes.reduce((m, n) => Math.max(m, n.x + n.w), 0);
  return { nodes, links, width, height: row * ROW };
}

/** What NotebookLM opens with: the root's children drawn, nothing deeper. */
export function initialOpen(): OpenSet {
  return new Set([pathKey([])]);
}

/** Every branch open. */
export function allOpen(root: MindNode): OpenSet {
  const out: OpenSet = new Set();
  const walk = (n: MindNode, path: NodePath) => {
    const kids = n.children ?? [];
    if (kids.length === 0) return;
    kids.forEach((c, i) => walk(c, [...path, i]));
    out.add(pathKey(path));
  };
  walk(root, []);
  return out;
}

/** The node at `path`, if there is one. */
export function nodeAt(root: MindNode, path: NodePath): MindNode | undefined {
  let n: MindNode | undefined = root;
  for (const i of path) {
    n = n.children?.[i];
    if (!n) return undefined;
  }
  return n;
}

/** The question a click on a node asks the chat. NotebookLM's own sentence,
 * word for word, from its shipped viewer: the node, and its direct parent
 * only, never the path above that. The root has no parent clause. */
export function questionFor(root: MindNode, path: NodePath): string | null {
  const node = nodeAt(root, path);
  if (!node) return null;
  if (path.length === 0) return `Discuss what these sources say about ${node.name}.`;
  const parent = nodeAt(root, path.slice(0, -1));
  if (!parent) return null;
  return `Discuss what these sources say about ${node.name}, in the larger context of ${parent.name}.`;
}

/** The map as an indented `- ` outline, the same format the model writes it
 * in. The Markdown export. */
export function toMarkdown(root: MindNode): string {
  let out = "";
  const walk = (n: MindNode, level: number) => {
    out += `${"  ".repeat(level)}- ${n.name}\n`;
    for (const c of n.children ?? []) walk(c, level + 1);
  };
  walk(root, 0);
  return out;
}

const escXml = (s: string) =>
  s.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");

/** The map as OPML 2.0, which mind map and outliner apps open. */
export function toOpml(root: MindNode): string {
  let out =
    `<?xml version="1.0" encoding="UTF-8"?>\n<opml version="2.0">\n` +
    `  <head><title>${escXml(root.name)}</title></head>\n  <body>\n`;
  const walk = (n: MindNode, level: number) => {
    const pad = "  ".repeat(level + 2);
    const kids = n.children ?? [];
    if (kids.length === 0) out += `${pad}<outline text="${escXml(n.name)}"/>\n`;
    else {
      out += `${pad}<outline text="${escXml(n.name)}">\n`;
      for (const c of kids) walk(c, level + 1);
      out += `${pad}</outline>\n`;
    }
  };
  walk(root, 0);
  return out + "  </body>\n</opml>\n";
}
