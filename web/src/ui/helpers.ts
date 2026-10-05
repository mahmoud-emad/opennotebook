// Small helpers the API modules and the pages share.

/** A list read again from the server, keeping the rows that did not change as
 * the very objects they were, so a row drawn from one is not drawn again for
 * nothing. Rows are matched by `key` and compared by their JSON. */
export function keepSame<T>(prev: T[], next: T[], key: (v: T) => string): T[] {
  if (prev.length === 0) return next;
  const old = new Map(prev.map((v) => [key(v), v]));
  let changed = prev.length !== next.length;
  const out = next.map((v, i) => {
    const was = old.get(key(v));
    const same = was !== undefined && JSON.stringify(was) === JSON.stringify(v);
    if (!same || prev[i] !== was) changed = true;
    return same ? was : v;
  });
  return changed ? out : prev;
}

/** `next`, or `prev` itself when they say the same. */
export function sameOr<T>(prev: T, next: T): T {
  return JSON.stringify(prev) === JSON.stringify(next) ? prev : next;
}

// ── reading the server's JSON ────────────────────────────────────────────────

/** A timestamp from the server as milliseconds; 0 for none. */
export const ms = (iso: string | null | undefined) => (iso ? Date.parse(iso) : 0);

/** A string field, or "" when it is anything else. */
export const str = (v: unknown): string => (typeof v === "string" ? v : "");

/** A number the server may send as a decimal string; 0 when it is neither. */
export const num = (v: unknown): number => (typeof v === "number" ? v : typeof v === "string" ? Number(v) || 0 : 0);

/** An output's kind as the pages name it: the server calls a deck "slides",
 * the pages (as the old app did) "session". Any other kind keeps its name. */
export const fromWireKind = (k: string): string => (k === "slides" ? "session" : k);

/** And back: the kind as the server takes it. */
export const toWireKind = <K extends string>(k: K): K | "slides" => (k === "session" ? "slides" : k);
