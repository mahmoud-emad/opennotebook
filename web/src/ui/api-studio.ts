// Every call the collection page makes that `api.ts` does not: outputs and
// their estimates, mind maps, study notes, the Ask conversation and research.
//
// As in `api.ts`, the screens read the shapes the old app read (a map's
// `created_ms`, a chat line with `who` and `me`), and this file is where the
// REST answers become those shapes. Routes the server has not ported yet
// answer with a sentence saying so, which `call` throws like any refusal, so
// the screen shows it where it shows every other error.

import type * as Rest from "@/client/types.gen";
import { apiBase, call, enc, postStream, serviceRoot, sessionOf, type SessionSummary } from "./api";
import type { Estimate } from "./dialogs";
import { citeFrom, type Cite } from "./markdown";
import type { MindNode } from "./mindmapLayout";

const ms = (iso: string | null | undefined) => (iso ? Date.parse(iso) : 0);
/** A number the server may send as a decimal string. */
const num = (v: unknown) => (typeof v === "number" ? v : typeof v === "string" ? Number(v) || 0 : 0);
const str = (v: unknown) => (typeof v === "string" ? v : "");

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
export async function estimateOutput(cid: string, req: BuildReq): Promise<Estimate> {
  return estimateOf(await call<Record<string, unknown>>("POST", `/collections/${enc(cid)}/outputs/estimate`, req));
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
  };
}

/** What a retry of a failed output keeps from it. */
export type SessionFacts = {
  title: string;
  collection: string;
  style: string | null;
  /** Slides of a deck, as far as its outline got; 0 for none. */
  slides: number;
  speakers: number;
  audio: { format: string; length: string; focus: string } | null;
};

export async function getSession(sid: string): Promise<SessionFacts> {
  const d = await call<Rest.SessionDetail>("GET", `/sessions/${enc(sid)}`);
  const a = d.audio;
  return {
    title: d.title,
    collection: d.collection_id,
    style: d.style ?? null,
    slides: d.slides.length,
    speakers: d.speakers,
    audio: a ? { format: str(a.format), length: str(a.length), focus: str(a.focus) } : null,
  };
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

// ── mind maps ────────────────────────────────────────────────────────────────

export type MindMapSummary = {
  id: string;
  title: string;
  focus: string;
  node_count: number;
  sources: string[];
  created_ms: number;
};

export type MindMap = MindMapSummary & { dropped: number; excerpted: boolean; root: MindNode };

/** What a map or notes would cost: one call over the sources, two at most
 * when the first answer comes back too thin. */
export type QuickEstimate = {
  priced: boolean;
  cost_usd: number;
  cost_high_usd: number;
  model: string;
  input_tokens: number;
  output_tokens: number;
  sources: number;
  chars: number;
  /** The spending limit it is checked against; 0 when there is none. */
  limit_usd: number;
  over_limit: boolean;
};

function quickOf(v: Record<string, unknown>): QuickEstimate {
  return {
    priced: v.priced === true,
    cost_usd: num(v.cost_usd),
    cost_high_usd: num(v.cost_high_usd),
    model: str(v.model),
    input_tokens: num(v.input_tokens),
    output_tokens: num(v.output_tokens),
    sources: num(v.sources),
    chars: num(v.chars),
    limit_usd: num(v.limit_usd),
    over_limit: v.over_limit === true,
  };
}

function mapSummaryOf(m: Rest.MindMapSummary): MindMapSummary {
  return {
    id: m.id,
    title: m.title,
    focus: m.focus,
    node_count: m.node_count,
    sources: m.sources,
    created_ms: ms(m.created_at),
  };
}

export function mapOf(m: Rest.MindMapOut): MindMap {
  return { ...mapSummaryOf(m), dropped: m.dropped, excerpted: m.excerpted, root: m.root };
}

const maps = (cid: string) => `/collections/${enc(cid)}/mindmaps`;

/** The collection's maps, newest first. */
export async function mindmapList(cid: string): Promise<MindMapSummary[]> {
  return (await call<Rest.MindMapSummary[]>("GET", maps(cid))).map(mapSummaryOf);
}

export async function mindmapGet(cid: string, id: string): Promise<MindMap> {
  return mapOf(await call<Rest.MindMapOut>("GET", `${maps(cid)}/${enc(id)}`));
}

export async function mindmapCreate(cid: string, focus: string): Promise<MindMap> {
  return mapOf(await call<Rest.MindMapOut>("POST", maps(cid), { focus } satisfies Rest.MakeReq));
}

export async function mindmapEstimate(cid: string): Promise<QuickEstimate> {
  return quickOf(await call<Record<string, unknown>>("GET", `${maps(cid)}/estimate`));
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
  focus: string;
  sources: string[];
  created_ms: number;
  ideas: number;
  questions: number;
  terms: number;
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

function notesSummaryOf(n: Rest.NotesSummary): StudyNotesSummary {
  return {
    id: n.id,
    title: n.title,
    focus: n.focus,
    sources: n.sources,
    created_ms: ms(n.created_at),
    ideas: n.ideas,
    questions: n.questions,
    terms: n.terms,
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
export async function notesList(cid: string): Promise<StudyNotesSummary[]> {
  return (await call<Rest.NotesSummary[]>("GET", notes(cid))).map(notesSummaryOf);
}

export async function notesGet(cid: string, id: string): Promise<StudyNotes> {
  return notesOf(await call<Rest.NotesOut>("GET", `${notes(cid)}/${enc(id)}`));
}

export async function notesCreate(cid: string, focus: string): Promise<StudyNotes> {
  return notesOf(await call<Rest.NotesOut>("POST", notes(cid), { focus } satisfies Rest.MakeReq));
}

export async function notesEstimate(cid: string): Promise<QuickEstimate> {
  return quickOf(await call<Record<string, unknown>>("GET", `${notes(cid)}/estimate`));
}

export async function notesRetitle(cid: string, id: string, title: string): Promise<void> {
  await call("PATCH", `${notes(cid)}/${enc(id)}`, { title } satisfies Rest.Retitle);
}

export async function notesDelete(cid: string, id: string): Promise<void> {
  await call("DELETE", `${notes(cid)}/${enc(id)}`);
}

// ── research ─────────────────────────────────────────────────────────────────

/** Research a topic in depth; the server adds the written report as one
 * source when it is done. Answers as soon as the work has started. */
export async function researchTopic(cid: string, topic: string): Promise<void> {
  await call("POST", `/collections/${enc(cid)}/research`, { topic } satisfies Rest.ResearchReq);
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
};

export function said(who: string, text: string, me: boolean): Msg {
  return { who, text, me, kind: "", id: "", detail: "", note: "", status: "", cites: [] };
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
export async function chatHistory(cid: string): Promise<Msg[]> {
  const rows = await call<Rest.Message[]>("GET", `/collections/${enc(cid)}/chat`);
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
): Promise<void> {
  return postStream(`/collections/${enc(cid)}/chat`, { ...picks, text } satisfies Rest.Say, onEvent);
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
): Promise<void> {
  return postStream(
    `/collections/${enc(cid)}/chat/commands`,
    { ...picks, name, arg, text: said } satisfies Rest.RunCommand,
    onEvent,
  );
}
