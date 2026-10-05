// Where the server is, and every way the app talks to it. The port of the old
// app's `api.rs`.
//
// The screens are ported from the old app as they were, so they read the
// shapes they always read (`cid`, `updated_ms`, a settings document with
// `tab_info` and `items`). This file is the one place that talks to the REST
// API and turns its answers into those shapes; the REST types come from the
// client generated from the server's OpenAPI document, so a server change
// breaks the build here rather than a page.

import type * as Rest from "@/client/types.gen";
import { UNKNOWN, UNREACHABLE, unworded } from "./errors";
import { fromWireKind, ms } from "./helpers";
import { sseFrames } from "./sse";

/** The service root this bundle was served under, with no trailing slash:
 * whatever precedes `/ui` in the address. */
export function serviceRoot(): string {
  const path = window.location.pathname;
  const i = path.indexOf("/ui");
  return i >= 0 ? path.slice(0, i) : "";
}

export function apiBase(): string {
  return `${serviceRoot()}/api`;
}

const UNREADABLE = "The studio's answer could not be read. Reload the page and try again.";

/** A request the server refused: its sentence, and the status it came with,
 * so a caller can tell "not there" (404) from any other refusal without
 * reading the words. */
export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Whether a call failed because what it asked for is not there. */
export const isGone = (e: unknown) => e instanceof ApiError && e.status === 404;

/** Whether a call was cancelled by its caller, which is not a failure to
 * show anyone: the page that asked has gone, or asked again. */
export const isAbort = (e: unknown) => e instanceof DOMException && e.name === "AbortError";

/** Send one request. A refusal comes back as the server's own sentence; a
 * request that never got an answer as the sentence for that. `init` carries
 * anything else for `fetch`, a `signal` to cancel it above all. */
export async function call<T>(
  method: string,
  path: string,
  body?: unknown,
  init: RequestInit = {},
): Promise<T> {
  let resp: Response;
  try {
    const isForm = body instanceof FormData;
    resp = await fetch(`${apiBase()}${path}`, {
      method,
      headers: body === undefined || isForm ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
      ...init,
    });
  } catch (e) {
    if (isAbort(e)) throw e;
    throw new Error(UNREACHABLE, { cause: e });
  }
  if (!resp.ok) throw new ApiError(await refusal(resp), resp.status);
  if (resp.status === 204) return undefined as T;
  try {
    return (await resp.json()) as T;
  } catch {
    throw new Error(UNREADABLE);
  }
}

/** What a refused request says, as the person should read it. */
export async function refusal(resp: Response): Promise<string> {
  try {
    const v = (await resp.json()) as { detail?: unknown };
    if (typeof v.detail === "string" && v.detail.trim()) return v.detail;
  } catch {
    // No body to read: the status says it.
  }
  return unworded(resp.status);
}

/** The message of anything thrown by a call, for a banner or a row. */
export function errText(e: unknown): string {
  return e instanceof Error ? e.message : UNKNOWN;
}

/** POST, then hand each server-sent event's JSON to `onEvent` as it arrives.
 * `EventSource` cannot POST, so the body is read as a stream and cut into
 * frames by `sseFrames`. */
export async function postStream(
  path: string,
  body: unknown,
  onEvent: (v: Record<string, unknown>) => void,
  signal?: AbortSignal,
): Promise<void> {
  let resp: Response;
  try {
    resp = await fetch(`${apiBase()}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (e) {
    if (isAbort(e)) throw e;
    throw new Error(UNREACHABLE, { cause: e });
  }
  if (!resp.ok) throw new ApiError(await refusal(resp), resp.status);
  const reader = resp.body?.getReader();
  if (!reader) throw new Error(UNREADABLE);
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch (e) {
      if (isAbort(e)) throw e;
      throw new Error("The answer was cut off. Try again.", { cause: e });
    }
    if (chunk.value) buf += dec.decode(chunk.value, { stream: true });
    const got = sseFrames(buf);
    buf = got.rest;
    for (const { data } of got.frames) {
      try {
        onEvent(JSON.parse(data) as Record<string, unknown>);
      } catch {
        // A keep-alive or a frame that is not JSON.
      }
    }
    if (chunk.done) return;
  }
}

/** This browser's local storage, where there is one. */
export function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/** Wait `ms`, or less: a `signal` that aborts ends the wait at once, so a loop
 * that sleeps stops with the page that runs it. */
export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise<void>((resolve) => {
    if (signal?.aborted) return resolve();
    const done = () => {
      clearTimeout(t);
      signal?.removeEventListener("abort", done);
      resolve();
    };
    const t = setTimeout(done, ms);
    signal?.addEventListener("abort", done, { once: true });
  });
}

// ── collections ──────────────────────────────────────────────────────────────

export type CollectionSummary = {
  cid: string;
  title: string;
  /** Its name as it is shown: the title, or "Untitled collection". */
  display_title: string;
  title_auto: boolean;
  created_ms: number;
  updated_ms: number;
  pinned: boolean;
  sources: number;
  decks: number;
  audios: number;
  maps: number;
  notes: number;
  preparing: number;
  failed: number;
  cover_version: string;
  /** The share this collection was copied from by a reuse; "" when none. */
  reused_from: string;
  /** It has a share, so everyone on this studio sees it in Discover. */
  shared: boolean;
  /** How many times its share was reused; 0 when it is not shared. */
  reuses: number;
  /** A copy whose share did not allow edits: read, asked, pinned and
   * deleted, never changed or shared. The server refuses the rest. */
  read_only: boolean;
  /** The name of the collection it was reused from, while that one is still
   * shared; null otherwise. */
  reused_from_title: string | null;
  /** Something in it is still being made: an output, its name or cover. */
  busy: boolean;
  /** The studio names it from its sources. */
  auto_named: boolean;
  /** What the studio is doing with its name, in words; null when none. */
  name_note: string | null;
};

function collectionOf(c: Rest.CollectionSummary): CollectionSummary {
  return {
    cid: c.id,
    title: c.title,
    display_title: c.display_title,
    title_auto: c.title_auto,
    created_ms: ms(c.created_at),
    updated_ms: ms(c.updated_at),
    pinned: c.pinned,
    sources: c.sources,
    decks: c.decks,
    audios: c.audios,
    maps: c.maps,
    notes: c.notes,
    preparing: c.preparing,
    failed: c.failed,
    cover_version: c.cover_version,
    reused_from: c.reused_from ?? "",
    shared: c.shared,
    reuses: c.reuses,
    read_only: c.read_only,
    reused_from_title: c.reused_from_title ?? null,
    busy: c.busy,
    auto_named: c.auto_named,
    name_note: c.name_note ?? null,
  };
}

/** One deck or audio overview, as the outputs list shows it. */
export type SessionSummary = {
  sid: string;
  title: string;
  /** Its name as it is shown: "Untitled narrated slides" while it has none. */
  display_title: string;
  state: string;
  slide_count: number;
  speakers: number;
  kind: string;
  audio_format: string;
  /** An audio overview's format as a person reads it; empty for a deck. */
  audio_label: string;
  duration_ms: number;
  description: string;
  created_ms: number;
  pinned: boolean;
  collection: string;
  spent_usd: number;
  spent_known: boolean;
  failure: string;
  /** The failure's technical detail, for whoever is debugging; empty when none. */
  failure_detail: string;
  /** Why a preparing output has not started, in a sentence; empty when it has. */
  waiting: string;
};

export function sessionOf(o: Rest.SessionSummary): SessionSummary {
  return {
    sid: o.id,
    title: o.title,
    display_title: o.display_title,
    state: o.state,
    slide_count: o.parts,
    speakers: o.speakers,
    kind: fromWireKind(o.kind) === "session" ? "session" : "audio",
    audio_format: o.audio_format,
    audio_label: o.audio_label,
    duration_ms: o.duration_ms,
    description: o.description,
    created_ms: ms(o.created_at),
    pinned: o.pinned,
    collection: o.collection_id,
    spent_usd: Number(o.spent_usd ?? 0),
    spent_known: o.spent_known,
    failure: o.failure ?? "",
    failure_detail: o.failure_detail ?? "",
    waiting: o.waiting ?? "",
  };
}

export async function listCollections(): Promise<CollectionSummary[]> {
  return (await call<Rest.CollectionSummary[]>("GET", "/collections")).map(collectionOf);
}

export async function getCollection(
  cid: string,
  signal?: AbortSignal,
): Promise<{ found: boolean; collection?: CollectionSummary; outputs: SessionSummary[] }> {
  try {
    const d = await call<Rest.CollectionDetail>("GET", `/collections/${enc(cid)}`, undefined, { signal });
    return {
      found: true,
      collection: collectionOf(d.collection),
      outputs: d.outputs.map(sessionOf),
    };
  } catch (e) {
    if (isGone(e)) return { found: false, outputs: [] };
    throw e;
  }
}

/** How far an output being made is, as a collection's stream says it. */
export type OutputProgress = { session_id: string; step: string; steps_done: number; steps_total: number };

/** What a collection's event stream says (`GET /api/collections/{cid}/events`),
 * each part when the stream opens and again when it changes. */
export type CollectionEvents = {
  collection?: (c: CollectionSummary) => void;
  outputs?: (o: SessionSummary[]) => void;
  progress?: (p: OutputProgress) => void;
  sources?: (s: ServerSource[]) => void;
  /** The collection was deleted. */
  gone?: () => void;
  /** Whether the stream is up: false when it dropped (the browser connects
   * again on its own), true once it is back. */
  up?: (ok: boolean) => void;
};

/** Follow a collection as the server says it changes, until the returned
 * function is called. One connection for the whole page. */
export function followCollection(cid: string, on: CollectionEvents): () => void {
  let es: EventSource;
  try {
    es = new EventSource(`${apiBase()}/collections/${enc(cid)}/events`);
  } catch {
    on.up?.(false);
    return () => {};
  }
  const listen = <T,>(name: string, use: (v: T) => void) =>
    es.addEventListener(name, (e: MessageEvent) => {
      let v: T;
      try {
        v = JSON.parse(String(e.data)) as T;
      } catch {
        return;
      }
      use(v);
    });
  listen<Rest.CollectionSummary>("collection", (v) => on.collection?.(collectionOf(v)));
  listen<Rest.SessionSummary[]>("outputs", (v) => on.outputs?.(v.map(sessionOf)));
  listen<OutputProgress>("progress", (v) => on.progress?.(v));
  listen<Rest.SourceOut[]>("sources", (v) => on.sources?.(v.map(serverSourceOf)));
  listen<unknown>("gone", () => {
    es.close();
    on.gone?.();
  });
  es.onopen = () => on.up?.(true);
  es.onerror = () => on.up?.(false);
  return () => es.close();
}

/** Start an empty collection and return its id. */
export async function createCollection(): Promise<string> {
  return (await call<Rest.CollectionSummary>("POST", "/collections", {})).id;
}

/** Rename a collection. Empty hands the naming back to the studio. */
export async function collectionRetitle(cid: string, title: string): Promise<void> {
  await call("PATCH", `/collections/${enc(cid)}`, { title });
}

export async function collectionPin(cid: string, pinned: boolean): Promise<void> {
  await call("PATCH", `/collections/${enc(cid)}`, { pinned });
}

export async function collectionCoverRefresh(cid: string): Promise<void> {
  await call("POST", `/collections/${enc(cid)}/cover`);
}

/** Delete a collection with everything in it. */
export async function collectionDelete(cid: string): Promise<void> {
  await call("DELETE", `/collections/${enc(cid)}`);
}

/** Where a collection's cover is drawn, in the viewer's theme. The version and
 * the theme are part of the address, so the browser keeps a cover until either
 * changes. */
export function coverUrl(cid: string, version: string, theme: "dark" | "light"): string {
  return `${apiBase()}/collections/${enc(cid)}/cover?v=${enc(version)}&theme=${theme}`;
}

// ── sources ──────────────────────────────────────────────────────────────────

/** A source as the server holds it, with the line under its name and its icon
 * as the server words them. */
export type ServerSource = { name: string; title: string; url: string; chars: number; detail: string; icon: string };

const serverSourceOf = (s: Rest.SourceOut): ServerSource => ({
  name: s.name,
  title: s.title,
  url: s.url,
  chars: s.chars,
  detail: s.detail,
  icon: s.icon,
});

export async function sourceList(cid: string, signal?: AbortSignal): Promise<ServerSource[]> {
  return (await call<Rest.SourceOut[]>("GET", `/collections/${enc(cid)}/sources`, undefined, { signal })).map(
    serverSourceOf,
  );
}

/** One thing asked to be added, as the old `Fetched` shape the rows read. */
export type Fetched = {
  ok: boolean;
  url: string;
  title: string;
  chars: number;
  error: string;
  name: string;
  icon: string;
};

function fetchedOf(r: Rest.AddResult): Fetched {
  return {
    ok: r.ok,
    url: r.url || r.source?.url || "",
    title: r.source?.title ?? "",
    chars: r.source?.chars ?? 0,
    error: r.error,
    name: r.source?.name ?? "",
    icon: "",
  };
}

export async function sourceAddUrls(cid: string, urls: string[]): Promise<Fetched[]> {
  const out = await call<Rest.AddResult[]>("POST", `/collections/${enc(cid)}/sources`, {
    kind: "urls",
    urls,
  });
  return out.map(fetchedOf);
}

export async function sourceAddText(cid: string, text: string, title = ""): Promise<Fetched> {
  const out = await call<Rest.AddResult[]>("POST", `/collections/${enc(cid)}/sources`, {
    kind: "text",
    text,
    title,
  });
  return fetchedOf(only(out));
}

/** The one result of an add of one thing. An empty answer is the studio not
 * saying, which is said as such rather than read as a success. */
function only(out: Rest.AddResult[]): Rest.AddResult {
  const r = out[0];
  if (!r) throw new Error(UNSAID);
  return r;
}

const UNSAID = "The studio did not say whether it was added. Reload the page to see your sources.";

export async function sourceAddFile(cid: string, file: File): Promise<Fetched> {
  const form = new FormData();
  form.append("files", file);
  const out = await call<Rest.AddResult[]>("POST", `/collections/${enc(cid)}/sources/files`, form);
  const r = fetchedOf(only(out));
  if (!r.ok) throw new Error(r.error);
  return r;
}

export async function sourceRemove(cid: string, name: string): Promise<void> {
  await call("DELETE", `/collections/${enc(cid)}/sources/${enc(name)}`);
}

export async function sourceRead(
  cid: string,
  name: string,
): Promise<{ name: string; title: string; url: string; text: string }> {
  return call("GET", `/collections/${enc(cid)}/sources/${enc(name)}`);
}

// ── settings ─────────────────────────────────────────────────────────────────

/** The settings document in the shape the old page read it. */
export type SettingOpt = { value: string; label: string; hint: string };
export type SettingItem = {
  key: string;
  tab: string;
  group: string;
  label: string;
  help: string;
  kind: string;
  options: SettingOpt[];
  suggestions: string[];
  min: number | null;
  max: number | null;
  unit: string;
  model: boolean;
  price: string;
  default: string;
  value: string;
  advanced: boolean;
};
export type TabInfo = { id: string; label: string; note: string; advanced: boolean };
export type SettingsDoc = { tab_info: TabInfo[]; items: SettingItem[] };

function itemOf(s: Rest.Setting): SettingItem {
  return {
    key: s.key,
    tab: s.tab,
    group: s.group,
    label: s.label,
    help: s.help,
    kind: s.kind,
    options: s.options,
    suggestions: (s as { suggestions?: string[] }).suggestions ?? [],
    min: s.min ?? null,
    max: s.max ?? null,
    unit: s.unit,
    model: s.model,
    price: s.price,
    default: s.default,
    value: s.value,
    advanced: s.advanced,
  };
}

export async function settingsLoad(): Promise<SettingsDoc> {
  const d = await call<Rest.SettingsOut>("GET", "/settings");
  return { tab_info: d.tabs, items: d.settings.map(itemOf) };
}

/** Save one setting; an empty value resets it. Returns the saved item. */
export async function settingsSet(key: string, value: string): Promise<SettingItem> {
  return itemOf(await call<Rest.Setting>("PATCH", `/settings/${enc(key)}`, { value }));
}

export function enc(s: string): string {
  return encodeURIComponent(s);
}
