// Every call the collection page makes that `api.ts` does not: outputs and
// their estimates, mind maps, study notes, the Ask conversation and research.
//
// As in `api.ts`, the screens read the shapes the old app read (a map's
// `created_ms`, a chat line with `who` and `me`), and this file is where the
// REST answers become those shapes. A refusal is the server's own sentence,
// which `call` throws, so the screen shows it where it shows every other
// error.

import type * as Rest from "@/client/types.gen";
import {
  apiBase,
  call,
  callAllBack,
  enc,
  errText,
  isAbort,
  isGone,
  postStream,
  serviceRoot,
  sessionOf,
  sleep,
  type SessionSummary,
} from "./api";
import type { Estimate } from "./dialogs";
import { ms, num, str } from "./helpers";
import { citeFrom, type Cite } from "./cite";
import type { MindNode } from "./mindmapLayout";


// ── decks and audio overviews ────────────────────────────────────────────────

/** What a build asks for: the kind and what was picked; anything left out is
 * taken from the settings by the server, by the same plan as the estimate. */
export type BuildReq = Rest.BuildReq;

/** Start a deck or an audio overview; the server mints its id and answers
 * with its row, preparing. */
export async function buildOutput(cid: string, req: BuildReq): Promise<SessionSummary> {
  return sessionOf(await call<Rest.SessionSummary>("POST", `/collections/${enc(cid)}/outputs`, req));
}

/** What exactly that build would cost, itemised. */
export async function estimateOutput(cid: string, req: BuildReq, signal?: AbortSignal): Promise<Estimate> {
  return estimateOf(
    await call<Record<string, unknown>>("POST", `/collections/${enc(cid)}/outputs/estimate`, req, { signal }),
  );
}

function estimateOf(v: Record<string, unknown>): Estimate {
  const lines = Array.isArray(v.lines) ? (v.lines as Record<string, unknown>[]) : [];
  const priceOrNull = (x: unknown) => (x === null || x === undefined ? null : num(x));
  return {
    total_low_usd: num(v.total_low_usd),
    total_typical_usd: num(v.total_typical_usd),
    total_high_usd: num(v.total_high_usd),
    lines: lines.map((l) => ({
      group: str(l.group),
      step: str(l.step),
      detail: str(l.detail),
      model: str(l.model),
      via: str(l.via),
      calls_low: num(l.calls_low),
      calls_typical: num(l.calls_typical),
      calls_high: num(l.calls_high),
      input_tokens: num(l.input_tokens),
      output_tokens_low: num(l.output_tokens_low),
      output_tokens_typical: num(l.output_tokens_typical),
      output_tokens_high: num(l.output_tokens_high),
      cost_low_usd: num(l.cost_low_usd),
      cost_typical_usd: num(l.cost_typical_usd),
      cost_high_usd: num(l.cost_high_usd),
      free: l.free === true,
      unpriced: l.unpriced === true,
      price_in_per_million: priceOrNull(l.price_in_per_million),
      price_out_per_million: priceOrNull(l.price_out_per_million),
    })),
    assumptions: Array.isArray(v.assumptions) ? v.assumptions.map(str) : [],
    sources: num(v.sources),
    source_chars: num(v.source_chars),
    slides: num(v.slides),
    speakers: num(v.speakers),
    style: str(v.style),
    slides_tier: str(v.slides_tier),
    priced_at: str(v.priced_at),
    minutes: num(v.minutes),
    limit_usd: num(v.limit_usd),
    over_limit: v.over_limit === true,
    limit_note: typeof v.limit_note === "string" ? v.limit_note : null,
    model: str(v.model),
    facts: Array.isArray(v.facts) ? v.facts.map(str) : [],
  };
}

/** Make a failed output again with the options it was made with. One step on
 * the server: the new one starts and the failed one goes, or, refused, the
 * failed one stays with its reason. Answers with the new one's row. */
export async function retryOutput(sid: string): Promise<SessionSummary> {
  return sessionOf(await call<Rest.SessionSummary>("POST", `/sessions/${enc(sid)}/retry`));
}

export async function sessionRetitle(sid: string, title: string): Promise<void> {
  await call("PATCH", `/sessions/${enc(sid)}`, { title });
}

export async function sessionDelete(sid: string): Promise<void> {
  await call("DELETE", `/sessions/${enc(sid)}`);
}

/** Where a build's progress streams from while it prepares. */
export function sessionEventsUrl(sid: string): string {
  return `${apiBase()}/sessions/${enc(sid)}/events`;
}

/** Where a ready deck or audio overview plays. */
export function playerUrl(sid: string): string {
  return `${serviceRoot()}/ui/play/${enc(sid)}`;
}

// ── what the Create panel offers ─────────────────────────────────────────────

/** Everything the Create panel offers for a collection, as the server words
 * it from the person's settings. */
export type StudioOptions = Rest.StudioOptions;
export type StyleChoice = Rest.StyleChoice;

export async function studioOptions(cid: string, signal?: AbortSignal): Promise<StudioOptions> {
  return call<StudioOptions>("GET", `/collections/${enc(cid)}/options`, undefined, { signal });
}

/** The slide styles, the default first, each with its picture. */
export async function styleList(signal?: AbortSignal): Promise<StyleChoice[]> {
  return call<StyleChoice[]>("GET", "/styles", undefined, { signal });
}

/** Where a file of the web app is, from its path under the app's mount: a
 * style's picture. Under the mount, so it resolves on every page. */
export function assetUrl(path: string): string {
  return `${serviceRoot()}/ui/${path}`;
}

// ── mind maps ────────────────────────────────────────────────────────────────

export type MindMapSummary = {
  id: string;
  title: string;
  /** Its name as it is shown: "Untitled mind map" while it has none. */
  display_title: string;
  focus: string;
  node_count: number;
  sources: string[];
  created_ms: number;
  /** "making" while the server's job draws it; it is then "ready", or gone
   * with its job saying why. */
  state: MadeState;
  /** The job that makes it. */
  job_id: string | null;
};

/** Where a map or notes are: being made by the server's worker, or ready. */
export type MadeState = "making" | "ready";

/** What asking for a map or notes answers at once: the job that makes it,
 * to follow, and its row as the server lists it, "making". */
export type Started<T> = { job: Rest.JobOut; made: T };

export type MindMap = MindMapSummary & { dropped: number; excerpted: boolean; root: MindNode };

export function mapSummaryOf(m: Rest.MindMapSummary): MindMapSummary {
  return {
    id: m.id,
    title: m.title,
    display_title: m.display_title,
    focus: m.focus,
    node_count: m.node_count,
    sources: m.sources,
    created_ms: ms(m.created_at),
    state: m.state ?? "ready",
    job_id: m.job_id ?? null,
  };
}

export function mapOf(m: Rest.MindMapOut): MindMap {
  return { ...mapSummaryOf(m), dropped: m.dropped, excerpted: m.excerpted, root: m.root };
}

const maps = (cid: string) => `/collections/${enc(cid)}/mindmaps`;

/** The collection's maps, newest first. */
export async function mindmapList(cid: string, signal?: AbortSignal): Promise<MindMapSummary[]> {
  return (await call<Rest.MindMapSummary[]>("GET", maps(cid), undefined, { signal })).map(mapSummaryOf);
}

export async function mindmapGet(cid: string, id: string, signal?: AbortSignal): Promise<MindMap> {
  return mapOf(await call<Rest.MindMapOut>("GET", `${maps(cid)}/${enc(id)}`, undefined, { signal }));
}

/** Ask for a map: the server answers at once and its worker draws it. */
export async function mindmapCreate(cid: string, focus: string): Promise<Started<MindMapSummary>> {
  const r = await call<Rest.MakingMap>("POST", maps(cid), { focus } satisfies Rest.MakeReq);
  return { job: r.job, made: mapSummaryOf(r.mindmap) };
}

/** What a map would cost, itemised as a build's estimate is. */
export async function mindmapEstimate(cid: string, signal?: AbortSignal): Promise<Estimate> {
  return estimateOf(await call<Record<string, unknown>>("GET", `${maps(cid)}/estimate`, undefined, { signal }));
}

export async function mindmapRetitle(cid: string, id: string, title: string): Promise<void> {
  await call("PATCH", `${maps(cid)}/${enc(id)}`, { title } satisfies Rest.Retitle);
}

export async function mindmapDelete(cid: string, id: string): Promise<void> {
  await call("DELETE", `${maps(cid)}/${enc(id)}`);
}

// ── study notes ──────────────────────────────────────────────────────────────

export type StudyNotesSummary = {
  id: string;
  title: string;
  /** Its name as it is shown: "Untitled study notes" while it has none. */
  display_title: string;
  focus: string;
  sources: string[];
  created_ms: number;
  ideas: number;
  questions: number;
  terms: number;
  /** As a map's: "making" while the server's job writes them. */
  state: MadeState;
  job_id: string | null;
};

export type StudyNotes = {
  id: string;
  title: string;
  focus: string;
  dropped: number;
  unchecked: boolean;
  excerpted: boolean;
  overview: string;
  ideas: { heading: string; body: string }[];
  quiz: { question: string; answer: string }[];
  essays: string[];
  glossary: { term: string; definition: string }[];
  citations: Cite[];
  markdown: string;
};

export function notesSummaryOf(n: Rest.NotesSummary): StudyNotesSummary {
  return {
    id: n.id,
    title: n.title,
    display_title: n.display_title,
    focus: n.focus,
    sources: n.sources,
    created_ms: ms(n.created_at),
    ideas: n.ideas,
    questions: n.questions,
    terms: n.terms,
    state: n.state ?? "ready",
    job_id: n.job_id ?? null,
  };
}

export function notesOf(n: Rest.NotesOut): StudyNotes {
  return {
    id: n.id,
    title: n.title,
    focus: n.focus,
    dropped: n.dropped,
    unchecked: n.unchecked,
    excerpted: n.excerpted,
    overview: n.overview,
    ideas: n.idea_list,
    quiz: n.quiz,
    essays: n.essays,
    glossary: n.glossary,
    citations: n.citations.map(citeFrom).filter((c): c is Cite => c !== null),
    markdown: n.markdown,
  };
}

const notes = (cid: string) => `/collections/${enc(cid)}/notes`;

/** The collection's notes, newest first. */
export async function notesList(cid: string, signal?: AbortSignal): Promise<StudyNotesSummary[]> {
  return (await call<Rest.NotesSummary[]>("GET", notes(cid), undefined, { signal })).map(notesSummaryOf);
}

export async function notesGet(cid: string, id: string, signal?: AbortSignal): Promise<StudyNotes> {
  return notesOf(await call<Rest.NotesOut>("GET", `${notes(cid)}/${enc(id)}`, undefined, { signal }));
}

/** Ask for notes: the server answers at once and its worker writes them. */
export async function notesCreate(cid: string, focus: string): Promise<Started<StudyNotesSummary>> {
  const r = await call<Rest.MakingNotes>("POST", notes(cid), { focus } satisfies Rest.MakeReq);
  return { job: r.job, made: notesSummaryOf(r.notes) };
}

/** What notes would cost, itemised as a build's estimate is. */
export async function notesEstimate(cid: string, signal?: AbortSignal): Promise<Estimate> {
  return estimateOf(await call<Record<string, unknown>>("GET", `${notes(cid)}/estimate`, undefined, { signal }));
}

export async function notesRetitle(cid: string, id: string, title: string): Promise<void> {
  await call("PATCH", `${notes(cid)}/${enc(id)}`, { title } satisfies Rest.Retitle);
}

export async function notesDelete(cid: string, id: string): Promise<void> {
  await call("DELETE", `${notes(cid)}/${enc(id)}`);
}

// ── research ─────────────────────────────────────────────────────────────────

/** Research a topic in depth; the server adds the written report as one
 * source when it is done. Answers with the work as soon as it has started. */
export async function researchTopic(cid: string, topic: string): Promise<Rest.JobOut> {
  return call<Rest.JobOut>("POST", `/collections/${enc(cid)}/research`, { topic } satisfies Rest.ResearchReq);
}

/** A piece of background work as the server has it now. */
export async function getJob(id: string, signal?: AbortSignal): Promise<Rest.JobOut> {
  return call<Rest.JobOut>("GET", `/jobs/${enc(id)}`, undefined, { signal });
}

/** Follow background work until it ends, handing on what the server says it is
 * doing (its step, or why it has not started) as that changes. Resolves to
 * null once it is done, else to why it is not, in the server's words. A read
 * that fails is tried again on the next; work that is gone (its collection
 * was deleted) is said as such. Stops quietly, with null, when `signal`
 * aborts. */
export async function followJob(
  id: string,
  onSaid: (said: string) => void,
  signal?: AbortSignal,
  every = 2000,
): Promise<string | null> {
  let last = "";
  for (;;) {
    await sleep(every, signal);
    if (signal?.aborted) return null;
    let j: Rest.JobOut;
    try {
      j = await getJob(id, signal);
    } catch (e) {
      if (isAbort(e)) return null;
      if (isGone(e)) return errText(e);
      continue;
    }
    if (j.status === "done") return null;
    if (j.status === "failed" || j.status === "cancelled")
      return j.error ?? "It stopped before it finished. Try again.";
    const said = j.waiting ?? j.step;
    if (said !== last) onSaid((last = said));
  }
}

// ── the Ask conversation ─────────────────────────────────────────────────────

/** One line of the Ask tab's conversation: something said, or a line of the
 * agent's work. */
export type Msg = {
  who: string;
  text: string;
  me: boolean;
  /** "" for something said, "step" for a line of the agent's work. */
  kind: string;
  /** A step's id, so its progress and result land on the right line. */
  id: string;
  /** What the step acted on: a query, a site. */
  detail: string;
  /** The step's latest progress, then its result. */
  note: string;
  /** "run", "ok" or "bad". */
  status: string;
  /** The passages an answer from the sources cites, matching its `[n]`. */
  cites: Cite[];
  /** What the thread knows the line by, for as long as the page is open. A
   * step's progress changes the line but not its key. */
  key: string;
};

let lines = 0;

export function said(who: string, text: string, me: boolean): Msg {
  return { who, text, me, kind: "", id: "", detail: "", note: "", status: "", cites: [], key: `m${++lines}` };
}

/** A kept work line as the step it was. */
function stepOf(s: Record<string, unknown>): Msg {
  const status = str(s.status) || (s.ok === false ? "bad" : "ok");
  return {
    ...said("Studio", str(s.text), false),
    kind: "step",
    id: str(s.id),
    detail: str(s.detail),
    note: str(s.note),
    status,
  };
}

/** The collection's conversation as the server keeps it, oldest first: each
 * answer's work lines before it, as they were shown while it ran. */
export async function chatHistory(cid: string, signal?: AbortSignal): Promise<Msg[]> {
  const rows = await callAllBack<Rest.Message>(`/collections/${enc(cid)}/chat`, { signal });
  const out: Msg[] = [];
  for (const m of rows) {
    if (m.role === "user") {
      out.push(said("You", m.text, true));
      continue;
    }
    for (const s of m.steps) out.push(stepOf(s));
    const cites = m.citations.map(citeFrom).filter((c): c is Cite => c !== null);
    if (m.text !== "") out.push({ ...said("Studio", m.text, false), cites });
  }
  return out;
}

export async function chatClear(cid: string): Promise<void> {
  await call("DELETE", `/collections/${enc(cid)}/chat`);
}

/** The `/` commands, in menu order. */
export async function listCommands(): Promise<Rest.Command[]> {
  return call<Rest.Command[]>("GET", "/commands");
}

/** What the page has picked, which the server uses for whatever the person
 * does not say: the kind to make, a deck's style, an audio overview's format
 * and length. */
export type Picks = {
  output: "" | "session" | "audio" | "mindmap" | "notes";
  style?: string;
  audio_format?: Rest.Say["audio_format"];
  audio_length?: Rest.Say["audio_length"];
};

/** One turn: the events of the answer as they arrive (`thinking`, `step`,
 * `step_note`, `step_done`, `source`, `reply`, `build`, `state`). The server
 * does the work, starts what is asked for, and keeps the turn. */
export function chatSay(
  cid: string,
  text: string,
  picks: Picks,
  onEvent: (v: Record<string, unknown>) => void,
  signal?: AbortSignal,
): Promise<void> {
  return postStream(`/collections/${enc(cid)}/chat`, { ...picks, text } satisfies Rest.Say, onEvent, signal);
}

/** A `/` command run by the server, answering with the same events; `/clear`
 * answers `cleared`. `said` is what the conversation keeps as said, when it
 * is not the command as typed (a question asked from a mind map). */
export function chatCommand(
  cid: string,
  name: string,
  arg: string,
  picks: Picks,
  onEvent: (v: Record<string, unknown>) => void,
  said = "",
  signal?: AbortSignal,
): Promise<void> {
  return postStream(
    `/collections/${enc(cid)}/chat/commands`,
    { ...picks, name, arg, text: said } satisfies Rest.RunCommand,
    onEvent,
    signal,
  );
}
