// The tests run on Node, and vitest blanks a stylesheet imported as `?raw`,
// so the theme is read here, from the web/ directory the tests run in.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  DEPTH_COLOURS,
  GAP,
  LINK_COLOUR,
  ROW,
  TOGGLE_W,
  allOpen,
  boundsOf,
  colours,
  cy,
  estimateWidth,
  find,
  initialOpen,
  layout,
  linkD,
  pathKey,
  questionFor,
  toMarkdown,
  toOpml,
  type Layout,
  type MindNode,
  type Placed,
} from "./mindmapLayout";

// Ported from the old SDK's mindmap_layout tests.

const n = (name: string, children: MindNode[]): MindNode => ({ name, children });
const leaf = (name: string) => n(name, []);

/** Root, three branches, uneven depth, one long label. */
function sample(): MindNode {
  return n("Moshi", [
    n("Real-time dialogue framework", [
      n("Inner Monologue method", [leaf("Text tokens"), leaf("Audio tokens")]),
      leaf("Full-duplex dialogue"),
    ]),
    n("Latency", [leaf("Theoretical 160ms"), leaf("Practical 200ms")]),
    leaf("Limitations of current systems and why pipelines of components fail"),
  ]);
}

const overlaps = (a: Placed, b: Placed) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
const startsWith = (p: number[], pre: number[]) => pre.every((v, i) => p[i] === v);

function checkInvariants(l: Layout) {
  l.nodes.forEach((a, i) => {
    for (const b of l.nodes.slice(i + 1)) expect(overlaps(a, b), `${a.name} overlaps ${b.name}`).toBe(false);
  });
  for (const c of l.nodes) {
    if (c.path.length === 0) continue;
    const p = find(l, c.path.slice(0, -1));
    expect(p, "a drawn node's parent is drawn").toBeDefined();
    expect(c.x >= p!.x + p!.w + GAP - 0.01, `${c.name} is not right of ${p!.name}`).toBe(true);
  }
  for (const p of l.nodes.filter((p) => p.open)) {
    const kids = l.nodes.filter((c) => c.path.length === p.path.length + 1 && startsWith(c.path, p.path));
    const lo = cy(kids[0]!);
    const hi = cy(kids[kids.length - 1]!);
    expect(cy(p) >= lo - 0.01 && cy(p) <= hi + 0.01, `${p.name} is not between its children`).toBe(true);
  }
}

describe("mind map layout", () => {
  it("opens like NotebookLM: root and its children only", () => {
    const l = layout(sample(), initialOpen(), estimateWidth);
    expect(l.nodes[0]!.name).toBe("Moshi");
    expect(l.nodes.length).toBe(4);
    expect(l.nodes.slice(1).every((n) => n.depth === 1 && !n.open)).toBe(true);
    expect(l.links.length).toBe(3);
    checkInvariants(l);
  });

  it("fully open, nothing overlaps and every child is right of its parent", () => {
    const t = sample();
    const l = layout(t, allOpen(t), estimateWidth);
    expect(l.nodes.length).toBe(10);
    expect(l.links.length).toBe(9);
    checkInvariants(l);
    // Six drawn leaves, six rows.
    expect(l.height).toBe(6 * ROW);
  });

  it("makes a column as wide as its widest label", () => {
    const l = layout(sample(), initialOpen(), estimateWidth);
    const widest = Math.max(...l.nodes.filter((n) => n.depth === 1).map((n) => n.w));
    const long = find(l, [2])!;
    expect(long.w).toBe(widest);
    expect(l.width).toBe(long.x + widest);
  });

  it("closing a node hides exactly its descendants and moves nothing above it", () => {
    const t = sample();
    const open = allOpen(t);
    const before = layout(t, open, estimateWidth);
    open.delete(pathKey([0, 0]));
    const after = layout(t, open, estimateWidth);
    const gone = before.nodes.filter((b) => !find(after, b.path)).map((b) => b.name);
    expect(gone).toEqual(["Text tokens", "Audio tokens"]);
    // Above it in reading order, and not one of its ancestors: unmoved.
    const at = before.nodes.findIndex((n) => pathKey(n.path) === "0,0");
    for (const b of before.nodes.slice(0, at)) {
      if (startsWith([0, 0], b.path)) continue;
      expect(find(after, b.path)!.y, `${b.name} moved`).toBe(b.y);
    }
    checkInvariants(after);
  });

  it("gives a branch room for its toggle and a leaf none", () => {
    const t = n("R", [leaf("Same"), n("Same", [leaf("x")])]);
    const l = layout(t, initialOpen(), estimateWidth);
    expect(find(l, [1])!.w - find(l, [0])!.w).toBe(TOGGLE_W);
  });

  it("asks NotebookLM's sentence on a click", () => {
    const t = sample();
    expect(questionFor(t, [])).toBe("Discuss what these sources say about Moshi.");
    expect(questionFor(t, [0])).toBe(
      "Discuss what these sources say about Real-time dialogue framework, in the larger context of Moshi.",
    );
    // The direct parent only, never the grandparent.
    const deep = questionFor(t, [0, 0, 1])!;
    expect(deep).toBe(
      "Discuss what these sources say about Audio tokens, in the larger context of Inner Monologue method.",
    );
    expect(deep).not.toContain("Moshi");
    expect(questionFor(t, [9])).toBeNull();
  });

  it("fits a click to the node and its children", () => {
    const t = sample();
    const l = layout(t, allOpen(t), estimateWidth);
    const [x, y, w, h] = boundsOf(l, [1])!;
    expect(x).toBe(find(l, [1])!.x);
    for (const c of [find(l, [1, 0])!, find(l, [1, 1])!]) {
      expect(c.y >= y && c.y + c.h <= y + h + 0.01).toBe(true);
      expect(c.x + c.w <= x + w + 0.01).toBe(true);
    }
  });

  it("exports every node", () => {
    const t = sample();
    const md = toMarkdown(t);
    expect(md.trimEnd().split("\n").length).toBe(10);
    expect(md).toContain("\n    - Inner Monologue method\n");
    const opml = toOpml(n("A & <B>", [leaf('"q"')]));
    expect(opml).toContain("<title>A &amp; &lt;B&gt;</title>");
    expect(opml).toContain('<outline text="&quot;q&quot;"/>');
    expect(toOpml(t).match(/<outline/g)!.length).toBe(10);
  });

  /** Every colour the map uses is a token the theme defines, in both themes. */
  it("uses only theme tokens for its colours", () => {
    const css = String(readFileSync("src/styles/theme.css", "utf8"));
    const names = [...DEPTH_COLOURS.flat(), LINK_COLOUR];
    for (const v of names) {
      const name = v.replace(/^var\(/, "").replace(/\)$/, "");
      expect(css.split(`${name}:`).length - 1, `${name} is not set in both themes`).toBe(2);
    }
    expect(colours(9)).toEqual(colours(4));
  });

  it("bends a link halfway across the gap", () => {
    expect(linkD({ to: [0], x1: 0, y1: 10, x2: 100, y2: 50 })).toBe("M0.0,10.0 C50.0,10.0 50.0,50.0 100.0,50.0");
  });
});
