// A passage an answer cites, as the chat, the notes and the API read it.
// Kept apart from `markdown.ts`, so what only reads citations does not load
// the Markdown parser and the sanitiser with them.

import { str } from "./helpers";

/** One passage an answer cites, as the chat keeps it. */
export type Cite = {
  n: number;
  title: string;
  url: string;
  /** The source's file name in the collection: what a click opens. */
  name: string;
  excerpt: string;
};

/** A citation from the server's JSON, or null when it has no number. */
export function citeFrom(v: unknown): Cite | null {
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  if (typeof o.n !== "number" || o.n < 0) return null;
  return { n: Math.floor(o.n), title: str(o.title), url: str(o.url), name: str(o.name), excerpt: str(o.excerpt) };
}

/** The sources an answer cites, each once, in order of first citation, with
 * the numbers of its passages: [title, url, "1, 3, 5"]. Seven passages from
 * one paper are one source, not seven. */
export function citeGroups(cites: Cite[]): [string, string, string][] {
  const out: [string, string, number[]][] = [];
  for (const c of cites) {
    const g = out.find(([t, u]) => t === c.title && u === c.url);
    if (g) g[2].push(c.n);
    else out.push([c.title, c.url, [c.n]]);
  }
  return out.map(([t, u, ns]) => [t, u, ns.join(", ")]);
}
