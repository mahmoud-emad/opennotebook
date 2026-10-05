// Sharing and reuse: every call the Discover feed, a shared collection's page
// and the share dialog make, turned into the shapes the old app read (its
// `ShareCard`, `Share`, `ShareViewOutput`). Like `api.ts`, this is the one
// place that knows the REST shapes.
//
// What a share includes is read through the share's own routes, never the
// collection's: those stay its owner's.

import { apiBase, call, enc, isGone } from "./api";
import { mapOf, notesOf, playerUrl, type MindMap, type StudyNotes } from "./api-studio";
import { fromWireKind, ms, toWireKind } from "./helpers";
import type * as Rest from "@/client/types.gen";


/** One collection's share: what it includes. */
export type Share = {
  share_id: string;
  cid: string;
  include_sources: boolean;
  /** Output keys, in the order the owner picked them. */
  outputs: string[];
  note: string;
  created_ms: number;
  updated_ms: number;
  reuses: number;
  /** Copies made from it are their reuser's to change and share again;
   * otherwise each copy is read-only. */
  allow_edits: boolean;
};

/** A share as the feed shows it. Counts are of what the share includes and
 * still exists. */
export type ShareCard = {
  share_id: string;
  cid: string;
  /** The person asking shared it, so they can edit or stop it. */
  mine: boolean;
  title: string;
  note: string;
  cover_version: string;
  terms: string[];
  /** 0 when sources are not included. */
  sources: number;
  decks: number;
  audios: number;
  maps: number;
  notes: number;
  reuses: number;
  /** A copy made now is the reuser's to change; otherwise read-only. */
  allow_edits: boolean;
  created_ms: number;
  updated_ms: number;
};

export type SharedSource = { name: string; title: string; url: string; chars: number };

export type SharedOutput = {
  /** The output key, as in `Share.outputs`. */
  key: string;
  /** "session" | "audio" | "mindmap" | "notes". */
  kind: string;
  title: string;
  /** The session's id for a deck or audio overview; "" for a map or notes. */
  sid: string;
  /** "" for a session; the map's or notes' id. */
  id: string;
  slide_count: number;
  duration_ms: number;
  created_ms: number;
};

/** What Discover lists: whole collections, or the outputs inside them of
 * one kind. */
export type FeedKind = "all" | "session" | "audio" | "mindmap" | "notes";

/** One output a share includes, as Discover lists it on its own. */
export type SharedItem = {
  key: string;
  /** "session" | "audio" | "mindmap" | "notes", as {@link SharedOutput}. */
  kind: string;
  /** The deck's, audio overview's, map's or notes' id. */
  id: string;
  title: string;
  slide_count: number;
  duration_ms: number;
  created_ms: number;
  share_id: string;
  cid: string;
  collection_title: string;
  cover_version: string;
  shared_by: string;
  mine: boolean;
  reuses: number;
};

/** One page of items, and where the next starts; null on the last. */
export type SharedItems = { items: SharedItem[]; next: number | null };

/** One shared collection as a visitor sees it. */
export type ShareView = {
  found: boolean;
  card?: ShareCard;
  /** Empty when sources are not included. */
  sources: SharedSource[];
  outputs: SharedOutput[];
};

// ── from the REST shapes ─────────────────────────────────────────────────────

function shareOf(s: Rest.ShareOut): Share {
  return {
    share_id: s.id,
    cid: s.collection_id,
    include_sources: s.include_sources,
    outputs: s.outputs,
    note: s.note,
    created_ms: ms(s.created_at),
    updated_ms: ms(s.updated_at),
    reuses: s.reuses,
    allow_edits: s.allow_edits,
  };
}

function cardOf(c: Rest.ShareCard): ShareCard {
  return {
    share_id: c.id,
    cid: c.collection_id,
    mine: c.mine,
    title: c.title,
    note: c.note,
    cover_version: c.cover_version,
    terms: c.terms,
    sources: c.sources,
    decks: c.decks,
    audios: c.audios,
    maps: c.maps,
    notes: c.notes,
    reuses: c.reuses,
    allow_edits: c.allow_edits,
    created_ms: ms(c.created_at),
    updated_ms: ms(c.updated_at),
  };
}

function sharedOutputOf(o: Rest.SharedOutput): SharedOutput {
  const kind = fromWireKind(o.kind);
  const session = kind === "session" || kind === "audio";
  return {
    key: o.key,
    kind,
    title: o.title,
    sid: session ? o.id : "",
    id: session ? "" : o.id,
    slide_count: o.parts,
    duration_ms: o.duration_ms,
    created_ms: ms(o.created_at),
  };
}

/** The wire's name for a kind of output: a deck is "slides" there. */
export const wireKind = (k: Exclude<FeedKind, "all">) => toWireKind(k);

export function sharedItemOf(i: Rest.SharedItem): SharedItem {
  return {
    key: i.key,
    kind: fromWireKind(i.kind),
    id: i.id,
    title: i.title,
    slide_count: i.parts,
    duration_ms: i.duration_ms,
    created_ms: ms(i.created_at),
    share_id: i.share_id,
    cid: i.collection_id,
    collection_title: i.collection_title,
    cover_version: i.cover_version,
    shared_by: i.shared_by,
    mine: i.mine,
    reuses: i.reuses,
  };
}

// ── the calls ────────────────────────────────────────────────────────────────

/** Everything shared on this studio, in `sort`'s order ("newest" or
 * "reused"), matching `query`. */
export async function shareFeed(
  query: string,
  sort: "newest" | "reused",
  signal?: AbortSignal,
): Promise<ShareCard[]> {
  const q = query.trim();
  const params = new URLSearchParams({ sort });
  if (q !== "") params.set("query", q);
  return (await call<Rest.ShareCard[]>("GET", `/shares?${params.toString()}`, undefined, { signal })).map(cardOf);
}

/** How many items Discover asks for at a time. */
export const ITEMS_PAGE = 24;

/** One page of the outputs shares include, of one kind, in `sort`'s order,
 * matching `query`, from `offset` on. */
export async function shareItems(
  kind: Exclude<FeedKind, "all">,
  query: string,
  sort: "newest" | "reused",
  offset = 0,
  signal?: AbortSignal,
): Promise<SharedItems> {
  const q = query.trim();
  const params = new URLSearchParams({ kind: wireKind(kind), sort, limit: String(ITEMS_PAGE) });
  if (q !== "") params.set("query", q);
  if (offset > 0) params.set("offset", String(offset));
  const page = await call<Rest.SharedItems>("GET", `/shares/items?${params.toString()}`, undefined, { signal });
  return { items: page.items.map(sharedItemOf), next: page.next_offset };
}

/** One shared collection as a visitor sees it; `found` is false when it is
 * not shared any more. */
export async function shareView(shareId: string, signal?: AbortSignal): Promise<ShareView> {
  try {
    const v = await call<Rest.ShareView>("GET", `/shares/${enc(shareId)}`, undefined, { signal });
    return {
      found: true,
      card: cardOf(v.card),
      sources: v.sources.map((s) => ({ name: s.name, title: s.title, url: s.url, chars: s.chars })),
      outputs: v.outputs.map(sharedOutputOf),
    };
  } catch (e) {
    if (isGone(e)) return { found: false, sources: [], outputs: [] };
    throw e;
  }
}

/** Copy a share into a new collection of one's own; its cid. */
export async function collectionReuse(shareId: string): Promise<string> {
  return (await call<Rest.CollectionSummary>("POST", `/shares/${enc(shareId)}/reuse`)).id;
}

/** The share of one of your collections, if it has one. */
export async function shareGet(cid: string): Promise<Share | null> {
  const s = await call<Rest.ShareOut | null>("GET", `/collections/${enc(cid)}/share`);
  return s ? shareOf(s) : null;
}

/** Share a collection, or change what its share includes. */
export async function shareSet(
  cid: string,
  req: { include_sources: boolean; outputs: string[]; note: string; allow_edits: boolean },
): Promise<Share> {
  return shareOf(
    await call<Rest.ShareOut>("POST", `/collections/${enc(cid)}/shares`, req satisfies Rest.ShareSet),
  );
}

/** Stop sharing a collection. */
export async function shareRemove(shareId: string): Promise<void> {
  await call("DELETE", `/shares/${enc(shareId)}`);
}

/** Where a shared collection's cover is drawn, in the viewer's theme: through
 * the share, as the collection's own cover is its owner's. */
export function shareCoverUrl(shareId: string, version: string, theme: "dark" | "light"): string {
  return `${apiBase()}/shares/${enc(shareId)}/cover?v=${enc(version)}&theme=${theme}`;
}

/** A map a share includes, to read only. */
export async function sharedMindmap(shareId: string, id: string, signal?: AbortSignal): Promise<MindMap> {
  return mapOf(await call<Rest.MindMapOut>("GET", `/shares/${enc(shareId)}/mindmaps/${enc(id)}`, undefined, { signal }));
}

/** Study notes a share includes, to read only. */
export async function sharedNotes(shareId: string, id: string, signal?: AbortSignal): Promise<StudyNotes> {
  return notesOf(await call<Rest.NotesOut>("GET", `/shares/${enc(shareId)}/notes/${enc(id)}`, undefined, { signal }));
}

/** Where a deck or audio overview a share includes plays: the player, told to
 * read it through the share. */
export function sharedPlayerUrl(shareId: string, sid: string): string {
  return `${playerUrl(sid)}?share=${enc(shareId)}`;
}
