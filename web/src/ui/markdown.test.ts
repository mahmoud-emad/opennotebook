import { describe, expect, it } from "vitest";
import { citeGroups, type Cite } from "./cite";
import { mdToHtml, plainExcerpt, withChips } from "./markdown";

const cite = (c: Partial<Cite> & { n: number }): Cite => ({ title: "", url: "", name: "", excerpt: "", ...c });

// Ported from the old app's chat.rs md_tests.
describe("markdown", () => {
  it("renders Markdown and not HTML", () => {
    const h = mdToHtml("See **this**: [Docs](https://x.org/a)\n\n1. one\n2. two");
    expect(h).toContain("<strong>this</strong>");
    expect(h).toContain('href="https://x.org/a"');
    expect(h).toContain('target="_blank"');
    expect(h).toContain("<ol>");
    expect(h).toContain("<li>one</li>");
    expect(mdToHtml("<script>alert(1)</script>")).not.toContain("<script>");
    expect(mdToHtml("A [trap](javascript:alert(1)) link")).not.toContain('href="javascript');
  });

  it("draws tables and strikethrough, and leaves a bare address as text", () => {
    const h = mdToHtml("| a | b |\n|---|---|\n| 1 | 2 |\n\n~~gone~~ https://x.org");
    expect(h).toContain("<table>");
    expect(h).toContain("<del>gone</del>");
    expect(h).not.toContain('href="https://x.org"');
  });
});

// Ported from the old app's mindmap.rs tests.
describe("citations", () => {
  it("reads a Markdown passage as prose", () => {
    const src =
      "Web research: https://en.wikipedia.org/wiki/Construction_of_the_Egyptian_pyramids\n" +
      "| Severity | Category | Finding |\n|---|---|---|\n" +
      "| info | other | The counterweight theory lacks confirmation. |\n" +
      "- **info / other** — See [the page](https://x.org/a) for more.";
    expect(plainExcerpt(src)).toBe(
      "Severity · Category · Finding info · other · The counterweight theory lacks " +
        "confirmation. info / other — See the page for more.",
    );
    expect(plainExcerpt("## Heading\nplain   text")).toBe("Heading plain text");
  });

  it("replaces markers with chips and escapes the source", () => {
    const cites = [
      cite({ n: 1, title: "A <b>", url: "https://x.org/?a=1&b=2", excerpt: "one   two" }),
      cite({ n: 2, title: "Note", excerpt: "e" }),
    ];
    const h = withChips("<p>X [1]. Y [2][1]. Not [10].</p>", cites);
    expect(h.split('class="cite"').length - 1).toBe(3);
    expect(h).toContain("Not [10].");
    expect(h).toContain("A &lt;b&gt;");
    expect(h).toContain('href="https://x.org/?a=1&amp;b=2"');
    expect(h).toContain("<span>one two</span>");
    // A note has no link to follow.
    expect(h).toContain('>2<span class="cite-pop"');
  });

  it("opens a source from its chip, and a passage never grows chips", () => {
    const cites = [
      cite({ n: 1, title: "Report", name: "research_1.md", excerpt: 'As [2] says, "ramps".' }),
      cite({ n: 2, title: "Wiki", name: "wiki.md", url: "https://w.org", excerpt: "e" }),
    ];
    const h = withChips("<p>A [1] B [2]</p>", cites);
    expect(h.split('class="cite"').length - 1).toBe(2);
    expect(h).toContain('data-src="research_1.md"');
    expect(h).toContain('data-x="As [2] says, &quot;ramps&quot;."');
    // A source opens in the viewer, which links the page itself.
    expect(h).not.toContain("href=");
  });

  it("lists one source cited many times once", () => {
    const c = (n: number, t: string) => cite({ n, title: t });
    expect(citeGroups([c(1, "Moshi"), c(2, "Mimi"), c(3, "Moshi"), c(4, "Moshi")])).toEqual([
      ["Moshi", "", "1, 3, 4"],
      ["Mimi", "", "2"],
    ]);
  });
});
