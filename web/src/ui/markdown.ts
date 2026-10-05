// The Studio's Markdown as safe HTML, and the numbered citation chips every
// cited answer, mind map answer and set of study notes shows. A port of
// `md_to_html` from the old app's `chat.rs` and the citation half of its
// `mindmap.rs`. What a citation is lives in `cite.ts`, apart from the
// Markdown parser and the sanitiser, so reading one costs the first screen
// neither.

import DOMPurify from "dompurify";
import { Marked } from "marked";
import type { Cite } from "./cite";

export function esc(s: string): string {
  return s.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

const safe = (u: string) => {
  const l = u.trim().toLowerCase();
  return l.startsWith("http://") || l.startsWith("https://") || l.startsWith("mailto:");
};

// CommonMark with tables and strikethrough, as the old app's pulldown-cmark
// was set up. Raw HTML is shown as text rather than parsed, a link keeps its
// href only when it is http(s) or mailto, an image is reduced to its words,
// and a bare address stays text, as it did there.
const md = new Marked({ gfm: true, breaks: false, async: false });
md.use({
  tokenizer: {
    url() {
      return undefined;
    },
  },
  renderer: {
    html({ text }) {
      return esc(text);
    },
    image({ text }) {
      return esc(text);
    },
    link({ href, title, tokens }) {
      const inner = this.parser.parseInline(tokens);
      const h = safe(href) ? esc(href) : "";
      const t = title ? ` title="${esc(title)}"` : "";
      return `<a target="_blank" rel="noopener noreferrer" href="${h}"${t}>${inner}</a>`;
    },
  },
});

/** A chat reply's Markdown as safe HTML.
 *
 * The text comes from a model, so it is treated as untrusted: raw HTML in it
 * is shown as text rather than parsed, and a link keeps its href only when it
 * is http(s) or mailto, which rules out `javascript:` and `data:`. What comes
 * out is sanitized once more besides. Links open in a new tab, since leaving
 * this screen would drop the conversation. */
export function mdToHtml(src: string): string {
  const html = md.parse(src) as string;
  return DOMPurify.sanitize(html, { ADD_ATTR: ["target"] });
}

/** A passage as prose to read in a popover.
 *
 * Passages are cut from the sources' Markdown as they are, so a research
 * report's came out as "Web research: https://… | Severity | Category |
 * Finding | |---|---|---| | info | other | …": its header, a bare link and a
 * table, run together. Read here line by line: a table row becomes its cells
 * joined by " · ", its rule line and any bare link go, and headings, list
 * marks, emphasis and inline links are reduced to their words. */
export function plainExcerpt(src: string): string {
  const lines: string[] = [];
  for (const raw of src.split("\n")) {
    let l = raw.trim();
    if (l === "") continue;
    // A table's rule: only pipes, dashes, colons and spaces.
    if (l.includes("-") && /^[|\-: ]*$/.test(l)) continue;
    if (l.startsWith("|"))
      l = l
        .split("|")
        .map((c) => c.trim())
        .filter((c) => c !== "")
        .join(" · ");
    l = l.replace(/^[#>]+/, "").trimStart();
    for (const mark of ["- ", "* ", "+ "])
      if (l.startsWith(mark)) {
        l = l.slice(mark.length);
        break;
      }
    const words = l.split(/\s+/).filter((w) => w !== "" && !isLink(w));
    const text = inlineLinks(words.join(" "))
      .replaceAll("**", "")
      .replaceAll("__", "")
      .replaceAll("`", "")
      .trim();
    // What is left of "Web research: https://…" says nothing.
    if (text === "" || (text.endsWith(":") && text.split(/\s+/).length <= 3)) continue;
    lines.push(text);
  }
  return lines.join(" ");
}

/** A bare web address, maybe in brackets or ending a sentence. */
function isLink(w: string): boolean {
  const t = w.replace(/^[(<[]+/, "");
  return t.startsWith("http://") || t.startsWith("https://") || t.startsWith("www.");
}

/** `[words](url)` as its words. */
function inlineLinks(s: string): string {
  let out = "";
  let rest = s;
  for (;;) {
    const i = rest.indexOf("](");
    if (i < 0) break;
    const open = rest.slice(0, i).lastIndexOf("[");
    if (open < 0) break;
    const close = rest.slice(i + 2).indexOf(")");
    if (close < 0) break;
    out += rest.slice(0, open) + rest.slice(open + 1, i);
    rest = rest.slice(i + 2 + close + 1);
  }
  return out + rest;
}

/** One chip: the number, and a popover with the source's title and passage. */
function chip(c: Cite): string {
  let short = plainExcerpt(c.excerpt);
  const chars = [...short];
  if (chars.length > 320) short = `${chars.slice(0, 320).join("").trimEnd()}…`;
  let num = String(c.n);
  let open = "";
  if (c.name !== "") open = ` role="button" data-src="${esc(c.name)}" data-x="${esc(c.excerpt)}"`;
  else if (c.url !== "") num = `<a href="${esc(c.url)}" target="_blank" rel="noopener noreferrer">${c.n}</a>`;
  const t = esc(c.title);
  return (
    `<span class="cite" tabindex="0"${open} aria-label="Source ${c.n}: ${t}">${num}` +
    `<span class="cite-pop" role="tooltip"><b>${t}</b><span>${esc(short)}</span></span></span>`
  );
}

/** An answer's HTML with each `[n]` turned into a numbered chip. Hovering or
 * focusing a chip shows the source's title and the passage; clicking it, or
 * Enter on it, opens the source at that passage (`source.tsx`'s listener). A
 * chip saved before chips knew their source links to the page instead.
 *
 * One pass over the text: a chip carries its passage, and a passage can hold
 * a "[2]" of its own, which replacing one number after another would turn
 * into a chip inside a chip. */
export function withChips(html: string, cites: Cite[]): string {
  let out = "";
  let rest = html;
  for (;;) {
    const i = rest.indexOf("[");
    if (i < 0) break;
    out += rest.slice(0, i);
    const after = rest.slice(i + 1);
    const digits = /^[0-9]*/.exec(after)![0].length;
    const cite =
      digits > 0 && after.slice(digits).startsWith("]")
        ? cites.find((c) => c.n === Number(after.slice(0, digits)))
        : undefined;
    if (cite) {
      out += chip(cite);
      rest = after.slice(digits + 1);
    } else {
      out += "[";
      rest = after;
    }
  }
  return out + rest;
}
