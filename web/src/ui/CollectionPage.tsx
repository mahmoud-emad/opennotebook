// One collection: its sources on the left, its Studio in the centre, and a
// map or a set of notes open on the right while one is. A port of the old
// app's `collection.rs`.
//
// The Studio is two halves of one column. Above, four tiles say what can be
// made; choosing one opens its options in place, under the tiles, and its one
// primary button makes it. Below, everything already made from these sources,
// newest first and every kind mixed, each saying where it is: preparing with
// its live step, failed with the reason and a retry, or ready to open.
//
// Nothing here waits on a build. A deck or an audio overview prepares in the
// background for minutes; its row follows the build's own event stream and
// the page polls the collection while anything is moving, so the person can
// start another output, read, or leave, and the work is where they left it.
//
// The chat is the Ask tab beside the Studio, about this collection's sources.
//
// The page's state lives in stores (`pageState`) rather than React state, as
// the old page's lived in signals: the work below runs for seconds or minutes
// and must read what is true when it lands, not what was true when it set out.

import { useEffect, useMemo, useState, type DragEvent } from "react";
import {
  UPLOAD_MAX_MB,
  collectionRetitle,
  errText,
  getCollection,
  sleep,
  sourceAddFile,
  sourceAddText,
  sourceAddUrls,
  sourceRemove,
  type CollectionSummary,
  type SessionSummary,
} from "./api";
import { buildOutput, estimateOutput, researchTopic, type BuildReq, type Picks } from "./api-studio";
import { AskTab, THREAD_ID, askSources, send, useChatState, type ChatState, type ChatMade } from "./chat";
import { CostDialog, LimitNote, anyUnpriced, countShort, usd, usdRange, usdRangeSpoken, type Estimate } from "./dialogs";
import { Cover, collTitle, coverPending } from "./home";
import { Icon } from "./Icon";
import { MindMapView, coveringMap, estimateMap, loadMaps, makeMap, newMapState } from "./mindmap";
import { NotesView, counts, coveringNotes, estimateNotes, installCiteFlip, loadNotes, makeNotes, newNotesState } from "./notes";
import {
  ErrRow,
  ItemRow,
  LIVE_MAX,
  PendingRow,
  SessionRow,
  deleteOutput,
  madeCreated,
  madeKey,
  retitle,
  type Made,
  type Target,
} from "./outputs";
import { shareView } from "./api-share";
import { follow, routeUrl, type Open, type View } from "./routes";
import { READ_ONLY_TAIL, ShareDialog, readOnlyOf, reusedLine } from "./share";
import { SETTINGS, SettingsLink, keys, settingValue, thumbUrl, useSettings } from "./settings";
import { OUTPUTS, focusId, isBuild, outputBlurb, outputHint, outputIcon, outputLabel, report, type Output } from "./shell";
import { SourceDrawer, useCiteOpen } from "./source";
import { FETCHING, SrcRow, serverSources, srcFrom, type Src } from "./sources";
import { store, useStore } from "./store";
import { STYLES } from "./styles";

export type CollectionPageProps = {
  cid: string;
  open: Open | null;
  /** A kind to open the Studio on, from an old address that asked for one. */
  start: Output | null;
  /** The collection's title, for the breadcrumb. */
  setCrumb: (title: string) => void;
  onOpen: (o: Open | null) => void;
  onGone: () => void;
};

/** The centre column's two tabs. */
type Tab = "studio" | "ask";

const TABS: Record<Tab, { label: string; icon: string; tabId: string; panelId: string }> = {
  studio: { label: "Studio", icon: "stars", tabId: "tab-studio", panelId: "panel-studio" },
  // The Ask panel is the chat thread itself.
  ask: { label: "Ask", icon: "chat-dots", tabId: "tab-ask", panelId: THREAD_ID },
};

/** NotebookLM's four formats: id, name, what it is, and the lengths it offers
 * (none for Brief, which is always about two minutes). */
export const AUDIO_FORMATS: [string, string, string, string[]][] = [
  [
    "deep_dive",
    "Deep Dive",
    "Two hosts in a lively conversation that unpacks your sources",
    ["shorter", "default", "longer"],
  ],
  ["brief", "Brief", "One host, the key points in about two minutes", []],
  ["critique", "Critique", "An expert review of your sources, with constructive feedback", ["shorter", "default"]],
  ["debate", "Debate", "Two hosts argue different sides of what your sources raise", ["shorter", "default"]],
];

/** The lengths a format offers; none for Brief. */
export function offeredLengths(format: string): string[] {
  return AUDIO_FORMATS.find((f) => f[0] === format)?.[3] ?? [];
}

/** An audio overview's format and length as a person reads them: "Brief",
 * "Deep Dive · shorter". The default length goes unsaid. */
export function audioDesc(format: string, length: string): string {
  const name = AUDIO_FORMATS.find((f) => f[0] === format)?.[1] ?? format;
  return length !== "default" && offeredLengths(format).includes(length) ? `${name} · ${length}` : name;
}

/** What can be uploaded, in words. */
export const UPLOAD_KINDS = "PDF, Word, PowerPoint, Excel, Markdown, text or CSV";
/** What can be uploaded as a source, by extension: the file picker offers
 * these and nothing else is sent. The server reads the same list. */
export const UPLOAD_EXTS = ["pdf", "docx", "pptx", "xlsx", "md", "markdown", "txt", "csv"];
/** The picker's `accept`, from the same list. */
export const UPLOAD_ACCEPT = ".pdf,.docx,.pptx,.xlsx,.md,.markdown,.txt,.csv";
/** The largest file the server takes, in bytes. */
const UPLOAD_MAX = UPLOAD_MAX_MB * 1024 * 1024;
/** What can be uploaded, as the add box says it. */
const uploadHint = () => `PDF, Office, Markdown, text or CSV, up to ${UPLOAD_MAX_MB} MB. Or drop them here.`;
/** The most links one Add takes: the server's limit. More stay in the box for
 * the next Add rather than being dropped. */
const MAX_LINKS = 8;

/** Why a file would be refused, said before it is sent; null sends it. */
export function uploadProblem(name: string, size: number): string | null {
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
  if (!UPLOAD_EXTS.includes(ext)) return `not a file the studio reads: ${UPLOAD_KINDS}`;
  if (size > UPLOAD_MAX) return `${Math.ceil(size / (1024 * 1024))} MB, over the ${UPLOAD_MAX_MB} MB limit`;
  if (size === 0) return "the file is empty";
  return null;
}

type Cfg = ReturnType<typeof useSettings>;

/** The voices a deck from `nSrc` sources is read by, as the settings decide:
 * "Host and Expert", or "Host" alone. */
function deckVoices(cfg: Cfg, nSrc: number): string | null {
  const host = cfg.get(keys.SPEAKER1_NAME);
  const second = cfg.get(keys.SPEAKER2_NAME);
  const count = cfg.get(keys.SPEAKER_COUNT);
  if (host === undefined || second === undefined || count === undefined) return null;
  const two = count === "1" ? false : count === "2" ? true : nSrc >= 2;
  return two ? `${host} and ${second}` : host;
}

/** "5 slides · about 5 min · Host and Expert": what a deck is made with. */
function deckSummary(cfg: Cfg, nSrc: number): string | null {
  const slides = cfg.shown(keys.SLIDE_COUNT);
  const minutes = cfg.shown(keys.SESSION_MINUTES);
  const voices = deckVoices(cfg, nSrc);
  if (slides === undefined || minutes === undefined || voices === null) return null;
  return `${slides} · about ${minutes} · ${voices}`;
}

/** The output language when it is not English, the one the voices speak. */
function otherLanguage(cfg: Cfg): string | null {
  const l = cfg.get(keys.LANGUAGE);
  return l !== undefined && l !== "English" ? l : null;
}

/** Whether both voices speak any output language natively: Microsoft's
 * Multilingual voices do; Kokoro's and the other Microsoft voices keep an
 * English accent. */
function nativeVoices(cfg: Cfg): boolean {
  const voices = [cfg.get(keys.SPEAKER1_VOICE), cfg.get(keys.SPEAKER2_VOICE)];
  return voices.every((v) => (v ?? "").includes("Multilingual"));
}

/** How long Research a topic reads the web, by the depth setting. */
function researchTime(cfg: Cfg): string {
  return cfg.get(keys.RESEARCH_DEPTH) === "quick" ? "about a minute" : "a few minutes";
}

/** A source that did not arrive, kept as a row that says why until dismissed. */
function failedSrc(name: string, why: string): Src {
  return { icon: "", name, detail: why, ok: false, url: "", file: "" };
}

/** The sources a build would read: on the server, not being read, not failed. */
const staged = (srcs: Src[]) => srcs.filter((s) => s.ok && s.file !== "").map((s) => s.file);

/** Whether a drag carries files, as opposed to text, a link or an image
 * dragged off the page. */
const draggingFiles = (e: DragEvent) => e.dataTransfer.types.includes("Files");

const sameOpen = (a: Open | null, b: Open | null) => JSON.stringify(a) === JSON.stringify(b);

/** Everything the page holds, as stores its long-running work reads when it
 * lands. Made once per page. */
function pageState(props: CollectionPageProps) {
  return {
    props: store(props),
    // The collection and its decks and audio overviews.
    summary: store<CollectionSummary | null>(null),
    outputs: store<SessionSummary[]>([]),
    loaded: store(false),
    missing: store(false),
    loadErr: store(""),
    // The title field. Follows the server's name until the person types.
    title: store(""),
    typing: store(false),
    // Sources.
    srcs: store<Src[]>([]),
    srcsLoaded: store(false),
    srcsErr: store(""),
    // The add box, shared by Add source and Research a topic.
    draft: store(""),
    // Topics being researched, each shown as a row until its report lands.
    // By a number of their own, so the same topic asked twice is two rows.
    researching: store<[number, string][]>([]),
    // Files being uploaded and read, by a number of their own and their name,
    // each a row until it is a source or says why it is not.
    uploading: store<[number, string][]>([]),
    mm: newMapState(),
    nt: newNotesState(),
    // Making.
    tab: store<Tab>("studio"),
    // The tile whose options are open; null shows the tiles alone.
    chosen: store<Output | null>(props.start),
    style: store(STYLES[0]!.id),
    // An audio overview's three options: NotebookLM's format, length and
    // Customize box. Deep Dive at its default length is what NotebookLM makes
    // when nothing is chosen.
    audioFormat: store("deep_dive"),
    audioLength: store("default"),
    // The Create panel starts on the settings' style, format and length, and
    // follows them when they change, until the person picks their own here.
    picked: store(false),
    audioFocus: store(""),
    // The build call is in flight (seconds); the build itself is not waited on.
    generating: store(false),
    genErr: store(""),
    // Why the last action on one row failed, by row: `session:<id>`,
    // `map:<id>`, `notes:<id>`, `src:<file>`. Said under that row.
    rowErr: store<Record<string, string>>({}),
    // A build's estimated cost, fetched whenever what would be built changes.
    est: store<Estimate | null>(null),
    estErr: store(""),
    estLoading: store(false),
  };
}

type PageState = ReturnType<typeof pageState>;

/** A length the format does not offer falls back to its default. */
function fitLength(S: PageState): void {
  if (!offeredLengths(S.audioFormat.get()).includes(S.audioLength.get())) S.audioLength.set("default");
}

/** The page's work: loading, adding, making, renaming and deleting. Made once
 * per page, over its stores. */
function pageActions(cid: string, S: PageState, chat: ChatState) {
  const props = () => S.props.get();
  // Which read of the collection is the latest. A poll that set out before a
  // Generate and answers after it would put back a list without the new row,
  // which then reappears on the next poll: a flicker. So a reply only lands
  // when nothing newer has happened since it was asked for.
  let loadGen = 0;
  // Which estimate request is the latest. Replies come back in any order, and
  // only the newest one describes what Generate would build now.
  let estSeq = 0;
  let rowN = 0;
  // The icon each page declared while it was read, by its url: the server's
  // list does not carry them, so they are kept for as long as the page is.
  const icons = new Map<string, string>();
  let gone = false;
  const pushSrc = (s: Src) => S.srcs.set((v) => [...v, s]);

  const load = async () => {
    const my = ++loadGen;
    let got: Awaited<ReturnType<typeof getCollection>> | null = null;
    let err = "";
    try {
      got = await getCollection(cid);
    } catch (e) {
      err = errText(e);
    }
    if (loadGen !== my) return;
    if (got?.found) {
      const c = got.collection;
      if (c) {
        if (!S.typing.get()) S.title.set(c.title);
        props().setCrumb(c.title);
        S.summary.set(c);
      }
      S.outputs.set(got.outputs);
      S.loadErr.set("");
    } else if (got) S.missing.set(true);
    else S.loadErr.set(err);
    S.loaded.set(true);
  };

  // The server's list, with the rows it does not have kept after it: a page
  // still being read, or one that failed and says why.
  const loadSources = async () => {
    try {
      const list = await serverSources(cid);
      const local = S.srcs.get().filter((s) => !s.ok || s.detail === FETCHING);
      const next = list.map((s) => {
        const i = icons.get(s.url);
        return i ? { ...s, icon: i } : s;
      });
      S.srcs.set([...next, ...local]);
      S.srcsErr.set("");
    } catch (e) {
      S.srcsErr.set(errText(e));
    }
    S.srcsLoaded.set(true);
  };

  const reloadAll = () => {
    void load();
    void loadMaps(cid, S.mm);
    void loadNotes(cid, S.nt);
  };

  // One input for both kinds of source: anything that looks like a link is
  // fetched, everything else is kept as a note.
  const addSource = async () => {
    const raw = S.draft.get().trim();
    if (raw === "") return;
    const all = raw.split(/\s+/).filter((t) => t.startsWith("http://") || t.startsWith("https://"));
    const urls = all.slice(0, MAX_LINKS);
    // Past the most one Add takes, the rest wait in the box.
    S.draft.set(all.slice(MAX_LINKS).join("\n"));
    if (urls.length > 0) {
      S.srcs.set((v) => [...v, ...urls.map((u) => ({ icon: "", name: u, detail: FETCHING, ok: true, url: u, file: "" }))]);
      try {
        const got = await sourceAddUrls(cid, urls);
        S.srcs.set((v) => v.filter((s) => s.detail !== FETCHING));
        // A page that did not arrive has to be visible: a deck built without
        // it looks exactly like a deck built with it. The ones that did are
        // read back from the server below.
        for (const g of got) {
          const s = srcFrom(g);
          if (!s.ok) pushSrc(s);
          else if (s.icon !== "") icons.set(s.url, s.icon);
        }
      } catch (e) {
        S.srcs.set((v) => v.filter((s) => s.detail !== FETCHING));
        pushSrc(failedSrc("Those links could not be read", errText(e)));
      }
    } else {
      const name = raw.split(/\s+/).slice(0, 6).join(" ");
      try {
        const r = await sourceAddText(cid, raw, name);
        if (!r.ok) throw new Error(r.error);
      } catch (e) {
        pushSrc(failedSrc(name === "" ? "Note" : name, errText(e)));
      }
    }
    await loadSources();
    await load();
  };

  // The report of a research started on the server lands as a new source;
  // until then the topic's row stays. Null once it has landed, else why not.
  const awaitReport = async (before: Set<string>): Promise<string | null> => {
    const until = Date.now() + 15 * 60_000;
    while (!gone && Date.now() < until) {
      await sleep(5000);
      try {
        if ((await serverSources(cid)).some((s) => !before.has(s.file))) return null;
      } catch {
        // A poll that failed is tried again on the next.
      }
    }
    return gone ? null : "the research did not finish";
  };

  // Research a topic: the add box's text as a brief, read across the web for
  // about a minute and added as one written report.
  const research = async () => {
    const topic = S.draft.get().trim();
    if (topic === "") return;
    S.draft.set("");
    const n = ++rowN;
    S.researching.set((v) => [...v, [n, topic]]);
    let why: string | null;
    try {
      const before = new Set((await serverSources(cid)).map((s) => s.file));
      await researchTopic(cid, topic);
      why = await awaitReport(before);
    } catch (e) {
      why = errText(e);
    }
    S.researching.set((v) => v.filter((r) => r[0] !== n));
    if (why !== null) pushSrc(failedSrc(`Research: ${topic}`, why === "" ? "the research did not finish" : why));
    await loadSources();
    await load();
  };

  // One row's failure, set or cleared (an empty sentence clears it).
  const setRowErr = (key: string, text: string) =>
    S.rowErr.set((m) => {
      const next = { ...m };
      if (text === "") delete next[key];
      else next[key] = text;
      return next;
    });

  const removeSource = (s: Src) => {
    if (s.file === "") {
      const key = JSON.stringify(s);
      S.srcs.set((v) => v.filter((x) => JSON.stringify(x) !== key));
      return;
    }
    void (async () => {
      try {
        await sourceRemove(cid, s.file);
        setRowErr(`src:${s.file}`, "");
      } catch (e) {
        setRowErr(`src:${s.file}`, `It could not be removed. ${errText(e)}`);
      }
      await loadSources();
      await load();
    })();
  };

  // Files picked or dropped: refused at once when the studio could not read
  // them, otherwise sent one at a time, each row becoming its source as it
  // lands. The collection is looked at again after, for the name.
  const uploadFiles = async (files: File[]) => {
    const queue: [number, File][] = [];
    for (const f of files) {
      const why = uploadProblem(f.name, f.size);
      if (why !== null) {
        pushSrc(failedSrc(f.name, why));
        continue;
      }
      const n = ++rowN;
      S.uploading.set((v) => [...v, [n, f.name]]);
      queue.push([n, f]);
    }
    if (queue.length === 0) return;
    for (const [n, f] of queue) {
      let err: string | null = null;
      try {
        await sourceAddFile(cid, f);
      } catch (e) {
        err = errText(e);
      }
      // The list first, then the row out: no gap where the file is neither
      // being read nor listed.
      if (err === null) await loadSources();
      S.uploading.set((v) => v.filter((u) => u[0] !== n));
      if (err !== null) pushSrc(failedSrc(f.name, err));
    }
    await load();
  };

  // A deck, an audio overview, a map or notes: renamed in place, in the list
  // and in the viewer if it is open, without reading anything back.
  const rename = (t: Target, next: string) => {
    void (async () => {
      try {
        await retitle(cid, t, next);
        setRowErr(`${t.kind}:${t.id}`, "");
      } catch (e) {
        setRowErr(`${t.kind}:${t.id}`, `It could not be renamed. ${errText(e)}`);
        return;
      }
      if (t.kind === "session") S.outputs.set((v) => v.map((s) => (s.sid === t.id ? { ...s, title: next } : s)));
      else if (t.kind === "map") S.mm.maps.set((v) => v.map((m) => (m.id === t.id ? { ...m, title: next } : m)));
      else S.nt.notes.set((v) => v.map((n) => (n.id === t.id ? { ...n, title: next } : n)));
    })();
  };

  // Any output, deleted: closed first if it is the one open, then the lists
  // read back.
  const remove = (t: Target) => {
    const shown: Open | null = t.kind === "session" ? null : { kind: t.kind, id: t.id };
    if (shown && sameOpen(props().open, shown)) props().onOpen(null);
    void (async () => {
      try {
        await deleteOutput(cid, t);
        setRowErr(`${t.kind}:${t.id}`, "");
      } catch (e) {
        setRowErr(`${t.kind}:${t.id}`, `It could not be deleted. ${errText(e)}`);
      }
      if (t.kind === "map") await loadMaps(cid, S.mm);
      else if (t.kind === "notes") await loadNotes(cid, S.nt);
      await load();
    })();
  };

  // The title: given to the server on Enter or on leaving the field. Empty
  // hands the naming back to the studio.
  const commitTitle = () => {
    // Enter commits, and the blur that follows has nothing new to say.
    if (!S.typing.get()) return;
    S.typing.set(false);
    const t = S.title.get().trim();
    if (t === (S.summary.get()?.title.trim() ?? "")) return;
    void (async () => {
      try {
        await collectionRetitle(cid, t);
      } catch (e) {
        report(`The collection could not be renamed. ${errText(e)}`);
      }
      await load();
    })();
  };

  /** What Generate would build now, as the server takes it: what was picked
   * here, and the rest left to the settings by the same plan as the estimate,
   * so the two cannot disagree. */
  const buildReq = (kind: Output, title: string | null): BuildReq => {
    if (kind === "audio") {
      const focus = S.audioFocus.get().trim();
      return {
        kind: "audio",
        ...(title === null ? {} : { title }),
        audio_format: S.audioFormat.get() as BuildReq["audio_format"],
        audio_length: S.audioLength.get() as BuildReq["audio_length"],
        ...(focus === "" ? {} : { focus }),
      };
    }
    return { kind: "slides", ...(title === null ? {} : { title }), style: S.style.get() };
  };

  // Start a deck or an audio overview in this collection, from its sources
  // and what was picked here. Returns once the build is submitted; its row
  // follows it from there.
  const generateBuild = async (kind: Output, named: string | null) => {
    if (S.generating.get()) return;
    S.generating.set(true);
    S.genErr.set("");
    // Empty unless the agent named it: the server names an output by what
    // tells it from the others ("Editorial slides").
    const t = (named ?? "").trim();
    try {
      const row = await buildOutput(cid, buildReq(kind, t));
      // On the list at once, before the server's row is read back, and no
      // read already on its way may take it off again.
      ++loadGen;
      const title = row.title.trim() === "" ? (t === "" ? outputLabel[kind] : t) : row.title;
      S.outputs.set((v) => [{ ...row, title }, ...v.filter((s) => s.sid !== row.sid)]);
      S.chosen.set(null);
      S.audioFocus.set("");
      S.tab.set("studio");
      void load();
    } catch (e) {
      S.genErr.set(errText(e));
    }
    S.generating.set(false);
  };

  // A map or notes: one call of seconds. The row says so while it runs, and
  // what it made opens beside the Studio, in place of whatever was open when
  // it was asked for, unless something else was opened while it was made.
  const generateMap = async () => {
    S.chosen.set(null);
    const before = props().open;
    const id = await makeMap(cid, S.mm);
    if (id !== null && sameOpen(props().open, before)) props().onOpen({ kind: "map", id });
    await load();
  };
  const generateNotes = async () => {
    S.chosen.set(null);
    const before = props().open;
    const id = await makeNotes(cid, S.nt);
    if (id !== null && sameOpen(props().open, before)) props().onOpen({ kind: "notes", id });
    await load();
  };
  const generate = (kind: Output) => {
    if (isBuild(kind)) void generateBuild(kind, null);
    else if (kind === "mindmap") void generateMap();
    else void generateNotes();
  };

  // The estimate asks for exactly what Generate would build.
  const fetchEstimate = async () => {
    const my = ++estSeq;
    const kind = S.chosen.get();
    if (kind === null) return;
    if (staged(S.srcs.get()).length === 0 || !isBuild(kind)) {
      S.est.set(null);
      return;
    }
    S.estLoading.set(true);
    S.estErr.set("");
    try {
      const e = await estimateOutput(cid, buildReq(kind, null));
      // A newer request is out; its reply is the one to show.
      if (estSeq !== my) return;
      S.est.set(e);
    } catch (e) {
      if (estSeq !== my) return;
      S.est.set(null);
      S.estErr.set(errText(e));
    }
    S.estLoading.set(false);
  };
  // Anything still in flight is for a build no longer chosen.
  const dropEstimate = () => {
    ++estSeq;
    S.est.set(null);
    S.estLoading.set(false);
  };

  // What the page has picked, for what the chat makes when the person does
  // not say: the tile chosen, and its style or format and length.
  const picks = (): Picks => {
    const o = S.chosen.get();
    return {
      output: o ?? "",
      style: S.style.get(),
      audio_format: S.audioFormat.get() as Picks["audio_format"],
      audio_length: S.audioLength.get() as Picks["audio_length"],
    };
  };

  // Something the server made or started from the chat, by the agent or a
  // slash command: shown the way the Studio's Generate shows it. A deck or an
  // audio overview is on the outputs list in the Studio; a map or notes open
  // beside it, unless something else was opened while they were made.
  const made = (before: Open | null) => (m: ChatMade) => {
    S.chosen.set(null);
    if (isBuild(m.kind)) {
      S.audioFocus.set("");
      S.tab.set("studio");
      void load();
      return;
    }
    void (async () => {
      if (m.kind === "mindmap") await loadMaps(cid, S.mm);
      else await loadNotes(cid, S.nt);
      if (m.id !== "" && sameOpen(props().open, before))
        props().onOpen({ kind: m.kind === "mindmap" ? "map" : "notes", id: m.id });
    })();
  };

  // A message or a `/` command: the server does the work and keeps the turn.
  const sendChat = async (text: string) => {
    if (chat.talking.get()) return;
    const changed = await send(
      chat,
      text,
      picks(),
      // A page the agent read: a source row at once.
      (s) => {
        if (s.icon !== "") icons.set(s.url, s.icon);
        pushSrc(s);
      },
      made(props().open),
    );
    if (changed) {
      await loadSources();
      await load();
    }
  };

  // A click on a mind map topic: NotebookLM's question, answered from the
  // sources with citations, in the Ask tab.
  const askFromMap = async (question: string) => {
    if (question.trim() === "" || chat.talking.get()) return;
    S.tab.set("ask");
    await askSources(chat, question, picks());
  };

  const mount = () => {
    gone = false;
    // Not the last collection's name while this one's is on its way.
    props().setCrumb("");
    void load();
    void loadSources();
    void loadMaps(cid, S.mm);
    void loadNotes(cid, S.nt);
  };
  const dispose = () => {
    gone = true;
  };

  return {
    load,
    loadSources,
    reloadAll,
    addSource,
    research,
    removeSource,
    setRowErr,
    uploadFiles,
    rename,
    remove,
    commitTitle,
    generate,
    generateBuild,
    fetchEstimate,
    dropEstimate,
    sendChat,
    askFromMap,
    mount,
    dispose,
  };
}

export function CollectionPage(props: CollectionPageProps) {
  const { cid, open, onOpen, onGone } = props;
  const [S] = useState(() => pageState(props));
  const chat = useChatState(cid);
  const A = useMemo(() => pageActions(cid, S, chat), [cid, S, chat]);
  const cfg = useSettings();
  const [cited, setCited] = useCiteOpen();
  // Files dragged over the sources panel: the drop overlay shows while this
  // is above zero. A count, because entering a child leaves its parent.
  const [dragDepth, setDragDepth] = useState(0);
  // The sources beside an open map, or folded to the strip.
  const [srcOpen, setSrcOpen] = useState(false);
  const [estOpen, setEstOpen] = useState(false);
  // The share dialog, from the header's Share.
  const [sharing, setSharing] = useState(false);
  // A read-only copy's original: its title while its share is there, null
  // once it is not (or could not be read), undefined until it is known.
  const [origin, setOrigin] = useState<string | null | undefined>(undefined);

  // The latest props, for work that finishes later: a map made in the
  // background opens only if nothing else has been opened meanwhile.
  useEffect(() => S.props.set(props));

  useEffect(() => {
    A.mount();
    return A.dispose;
  }, [A]);
  useEffect(installCiteFlip, []);

  // While anything is moving, look again every few seconds: an output still
  // preparing, a name the studio has not given yet, or a cover still being
  // designed. Idle otherwise.
  useEffect(() => {
    let live = true;
    void (async () => {
      while (live) {
        await sleep(4000);
        if (!live) return;
        const doc = SETTINGS.get().doc;
        const c = S.summary.get();
        const preparing = S.outputs.get().some((s) => s.state === "preparing");
        const naming =
          settingValue(doc, keys.AUTO_NAME) !== "off" && !!c && c.title_auto && c.title.trim() === "" && c.sources > 0;
        const cover = !!c && coverPending(c, settingValue(doc, keys.COVERS) !== "off");
        if (preparing || naming || cover) await A.load();
      }
    })();
    return () => {
      live = false;
    };
  }, [A, S]);

  // The Create panel starts on the settings' style, format and length, and
  // follows them when they change, until the person picks their own here.
  const doc = cfg.doc;
  useEffect(() => {
    if (S.picked.get()) return;
    const v = settingValue(doc, keys.STYLE);
    if (v !== undefined && STYLES.some((s) => s.id === v)) S.style.set(v);
    const f = settingValue(doc, keys.AUDIO_FORMAT);
    if (f !== undefined && AUDIO_FORMATS.some((a) => a[0] === f)) S.audioFormat.set(f);
    const l = settingValue(doc, keys.AUDIO_LENGTH);
    if (l !== undefined) S.audioLength.set(l);
    fitLength(S);
  }, [S, doc]);

  const summary = useStore(S.summary);
  // A copy whose author did not allow edits: read, asked and played, never
  // changed or shared. The server refuses those; the page does not offer them.
  const ro = !!summary?.read_only;
  const originId = ro ? (summary?.reused_from ?? "") : "";
  useEffect(() => {
    if (originId === "") return;
    let live = true;
    shareView(originId).then(
      (v) => live && setOrigin(v.found && v.card ? collTitle(v.card.title) : null),
      () => live && setOrigin(null),
    );
    return () => {
      live = false;
    };
  }, [originId]);
  const outputs = useStore(S.outputs);
  const loaded = useStore(S.loaded);
  const missing = useStore(S.missing);
  const loadErr = useStore(S.loadErr);
  const title = useStore(S.title);
  const srcs = useStore(S.srcs);
  const srcsLoaded = useStore(S.srcsLoaded);
  const srcsErr = useStore(S.srcsErr);
  const draft = useStore(S.draft);
  const researching = useStore(S.researching);
  const uploading = useStore(S.uploading);
  const maps = useStore(S.mm.maps);
  const mmFocus = useStore(S.mm.focus);
  const mmMaking = useStore(S.mm.making);
  const mmErr = useStore(S.mm.err);
  const mmEst = useStore(S.mm.est);
  const mmEstLoading = useStore(S.mm.estLoading);
  const notes = useStore(S.nt.notes);
  const ntFocus = useStore(S.nt.focus);
  const ntMaking = useStore(S.nt.making);
  const ntErr = useStore(S.nt.err);
  const ntEst = useStore(S.nt.est);
  const ntEstLoading = useStore(S.nt.estLoading);
  const tab = useStore(S.tab);
  const kindNow = useStore(S.chosen);
  const style = useStore(S.style);
  const audioFormat = useStore(S.audioFormat);
  const audioLength = useStore(S.audioLength);
  const audioFocus = useStore(S.audioFocus);
  const generating = useStore(S.generating);
  const genErr = useStore(S.genErr);
  const rowErr = useStore(S.rowErr);
  const est = useStore(S.est);
  const estErr = useStore(S.estErr);
  const estLoading = useStore(S.estLoading);

  // The sources a build would read: on the server, not being read, not failed.
  const stagedNow = useMemo(() => staged(srcs), [srcs]);
  const nSrc = stagedNow.length;

  // Priced again when what would be made changes: an estimate for a
  // different build is worse than none. A change in Settings (slides,
  // voices, the limit) is a different build.
  useEffect(() => {
    if (ro) A.dropEstimate();
    else if (kindNow !== null && isBuild(kindNow) && nSrc > 0) void A.fetchEstimate();
    else if (kindNow === "mindmap" && nSrc > 0) void estimateMap(cid, S.mm);
    else if (kindNow === "notes" && nSrc > 0) void estimateNotes(cid, S.nt);
    else A.dropEstimate();
  }, [A, S, cid, ro, kindNow, nSrc, style, audioFormat, audioLength, doc]);

  // The map or notes already made from exactly these sources with no focus:
  // making another would say the same again, so the options say so.
  const freshMap = mmFocus.trim() === "" ? coveringMap(maps, stagedNow) : null;
  const freshNotes = ntFocus.trim() === "" ? coveringNotes(notes, stagedNow) : null;

  if (missing)
    return (
      <main>
        <div className="empty">
          <div className="empty-mark">
            <Icon name="collection" className="xl" />
          </div>
          <div className="empty-t">This collection is not here</div>
          <div className="empty-d">It may have been deleted, or the link is wrong.</div>
          <button onClick={onGone}>Back to My collections</button>
        </div>
      </main>
    );

  // Everything made, newest first, every kind in one list.
  const made: Made[] = [
    ...outputs.map((s): Made => ({ kind: "session", s })),
    ...maps.map((m): Made => ({ kind: "map", m })),
    ...notes.map((n): Made => ({ kind: "notes", n })),
  ].sort((a, b) => madeCreated(b) - madeCreated(a));
  // Live progress for the newest preparing builds only. Each live row holds
  // an EventSource, and a browser allows six HTTP/1.1 connections per host: a
  // stream per row starved every other request of the page once a few were
  // preparing. The rest say "Preparing" and catch up through the poll.
  const live = outputs
    .filter((s) => s.state === "preparing")
    .sort((a, b) => b.created_ms - a.created_ms)
    .slice(0, LIVE_MAX)
    .map((s) => s.sid);
  const nOut = made.length;
  // The build as chosen would be refused for its cost.
  const overLimit = !!est?.over_limit;
  // Settings the page says something about. Until they are read the hints
  // stay quiet rather than show a value that may not be the one in force.
  const naming = cfg.on(keys.AUTO_NAME) && !!summary?.title_auto;
  const showCost = cfg.on(keys.SHOW_COST);
  const researchTakes = researchTime(cfg);
  const language = otherLanguage(cfg);
  const topicTyped = draft.trim() !== "" && !draft.includes("http://") && !draft.includes("https://");
  const viewer = open !== null;
  const closeViewer = () => {
    onOpen(null);
    setSrcOpen(false);
  };

  return (
    <>
      <div className={viewer ? (srcOpen ? "create with-map show-src" : "create with-map") : "create two"}>
        {/* With a map open, the sources fold to this strip; pressing it opens
            them beside the map, which stays where it is. */}
        <div className="src-strip">
          <button
            title="Show the sources"
            aria-label="Show the sources"
            aria-expanded="false"
            onClick={() => setSrcOpen(true)}
          >
            <Icon name="link-45deg" />
            Sources
          </button>
        </div>

        {/* ── sources ── */}
        <aside
          className="panel sources"
          aria-label="Sources"
          // Files dropped anywhere on the panel are uploaded. Only a drag that
          // carries files is answered; text dragged about is not.
          onDragEnter={(e) => {
            if (ro || !draggingFiles(e)) return;
            e.preventDefault();
            setDragDepth((d) => d + 1);
          }}
          onDragOver={(e) => {
            if (ro || !draggingFiles(e)) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = "copy";
          }}
          onDragLeave={(e) => {
            if (draggingFiles(e)) setDragDepth((d) => Math.max(d - 1, 0));
          }}
          onDrop={(e) => {
            if (ro || !draggingFiles(e)) return;
            e.preventDefault();
            setDragDepth(0);
            void A.uploadFiles([...e.dataTransfer.files]);
          }}
        >
          {dragDepth > 0 && (
            <div className="src-drop" aria-hidden="true">
              <Icon name="upload" className="xl" />
              <span>Drop files to add them</span>
            </div>
          )}
          <div className="src-head">
            <h2>
              Sources{nSrc > 0 && <span className="h-n">{` ${nSrc}`}</span>}
            </h2>
            <button
              className="icon-btn src-fold"
              title="Fold the sources away"
              aria-label="Fold the sources away"
              aria-expanded="true"
              onClick={() => setSrcOpen(false)}
            >
              <Icon name="chevron-left" />
            </button>
          </div>
          {!ro && (
            <>
              <textarea
                aria-label="A link, some text, or a topic"
                value={draft}
                placeholder={`Paste up to ${MAX_LINKS} links, text, or a topic to research`}
                onChange={(e) => S.draft.set(e.target.value)}
              />
              <div className="src-actions" role="group" aria-label="Add sources">
                <button disabled={draft.trim() === ""} onClick={() => void A.addSource()}>
                  <Icon name="plus-lg" />
                  Add source
                </button>
                <button
                  className="ghost"
                  title={`Read the web on this topic for ${researchTakes} and add a written report`}
                  disabled={draft.trim() === ""}
                  onClick={() => void A.research()}
                >
                  <Icon name="search" />
                  Research a topic
                </button>
                <button
                  className="src-upload"
                  title={`PDF, Word, PowerPoint, Excel, Markdown, text or CSV, up to ${UPLOAD_MAX_MB} MB each. Or drop files on this panel.`}
                  onClick={() => document.getElementById("src-files")?.click()}
                >
                  <Icon name="upload" />
                  Upload files
                </button>
                <p className="src-upload-d">{uploadHint()}</p>
              </div>
              {/* A topic in the box: what Research a topic will do with it. */}
              {topicTyped && cfg.loaded && (
                <p className="src-hint">
                  {researchTakes === "about a minute" ? "Quick research" : "Standard research"}
                  {` takes ${researchTakes}. `}
                  <SettingsLink tab="defaults" text="Change in Settings › Generation defaults" />
                </p>
              )}
              <input
                id="src-files"
                type="file"
                multiple
                accept={UPLOAD_ACCEPT}
                hidden
                tabIndex={-1}
                aria-hidden="true"
                onChange={(e) => {
                  const input = e.currentTarget;
                  const files = [...(input.files ?? [])];
                  // Emptied, so picking the same file again is a change.
                  input.value = "";
                  void A.uploadFiles(files);
                }}
              />
            </>
          )}
          <div className="srclist" aria-live="polite">
            {uploading.map(([n, name]) => (
              <div key={`u-${n}`} className="src run">
                <span className="src-i spin" title="Reading…" />
                <div className="src-t">
                  <div className="src-n" title={name}>{`Reading ${name}…`}</div>
                  <div className="src-d">Uploading and reading the file</div>
                </div>
              </div>
            ))}
            {researching.map(([n, topic]) => (
              <div key={`r-${n}`} className="src run">
                <span className="src-i spin" title="Researching…" />
                <div className="src-t">
                  <div className="src-n">{`Researching: ${topic}`}</div>
                  <div className="src-d">{`Reading the web · ${researchTakes}`}</div>
                </div>
              </div>
            ))}
            {!srcsLoaded ? (
              <div className="src-note">
                <span className="mini-spin" />
                Loading sources…
              </div>
            ) : srcsErr !== "" && srcs.length === 0 ? (
              <div className="src-note bad" role="alert">
                {`Sources could not be loaded: ${srcsErr}`}
                <button className="link-btn" onClick={() => void A.loadSources()}>
                  Try again
                </button>
              </div>
            ) : srcs.length === 0 && researching.length === 0 && uploading.length === 0 ? (
              <div className="src-empty">
                <div className="src-empty-m">
                  <Icon name="link-45deg" className="xl" />
                </div>
                <div className="src-empty-t">No sources yet</div>
                <div className="dim small">
                  {ro
                    ? "Its author shared it without its sources."
                    : "Paste links or text, upload files, or research a topic. Everything you make here is made from these."}
                </div>
              </div>
            ) : (
              srcs.map((s, n) => (
                <SrcRow
                  key={`${n}-${s.file}-${s.icon}`}
                  s={s}
                  onRemove={ro ? undefined : A.removeSource}
                  err={rowErr[`src:${s.file}`] ?? ""}
                  onDismissErr={() => A.setRowErr(`src:${s.file}`, "")}
                />
              ))
            )}
          </div>
        </aside>

        {/* ── the Studio and Ask ── */}
        <section className="panel studio">
          <div className="studio-head">
            {summary && <Cover cid={summary.cid} version={summary.cover_version} size="head" />}
            <div className="sh-main">
              <input
                className="make-title"
                type="text"
                aria-label="Collection name"
                title={
                  ro
                    ? "A read-only copy keeps its name"
                    : naming
                      ? "Named from its sources until you name it. Turn off in Settings › Generation defaults."
                      : "Name this collection"
                }
                readOnly={ro}
                placeholder="Untitled collection"
                maxLength={120}
                value={title}
                onChange={(e) => {
                  S.typing.set(true);
                  S.title.set(e.target.value);
                }}
                onKeyDown={(e) => e.key === "Enter" && A.commitTitle()}
                onBlur={A.commitTitle}
              />
              <div className="sh-sub num">
                <span>{nSrc === 1 ? "1 source" : `${nSrc} sources`}</span>
                <span>·</span>
                <span>{nOut === 1 ? "1 output" : `${nOut} outputs`}</span>
                {naming && title.trim() === "" && nSrc > 0 ? (
                  <>
                    <span>·</span>
                    <span className="sh-auto">Naming it from its sources…</span>
                  </>
                ) : naming && title.trim() !== "" ? (
                  <>
                    <span>·</span>
                    <span className="sh-auto">Named from its sources</span>
                  </>
                ) : null}
                {summary?.shared && (
                  <>
                    <span>·</span>
                    <span>{reusedLine(summary.reuses)}</span>
                  </>
                )}
              </div>
              {ro && <ReadOnlyNote id={originId} origin={origin} />}
            </div>
            {/* Secondary: this page's one primary is its Generate. */}
            {summary && !ro && (
              <button
                className="share-btn"
                title={
                  summary.shared
                    ? "Shared with everyone on this studio. Change what it includes, or stop sharing."
                    : "Let everyone on this studio see it and reuse a copy"
                }
                aria-haspopup="dialog"
                onClick={() => setSharing(true)}
              >
                <Icon name="share" />
                {summary.shared ? "Shared" : "Share"}
              </button>
            )}
            {/* A tablist as the ARIA pattern has it: arrow keys move between
                the tabs, only the selected one is in the Tab order, and each
                names the panel it shows. */}
            <div
              className="seg"
              role="tablist"
              aria-label="View"
              onKeyDown={(e) => {
                const next: Tab | null =
                  e.key === "ArrowLeft" || e.key === "Home"
                    ? "studio"
                    : e.key === "ArrowRight" || e.key === "End"
                      ? "ask"
                      : null;
                if (next === null) return;
                e.preventDefault();
                S.tab.set(next);
                focusId(TABS[next].tabId);
              }}
            >
              {(["studio", "ask"] as Tab[]).map((t) => (
                <button
                  key={TABS[t].tabId}
                  id={TABS[t].tabId}
                  className={tab === t ? "on" : ""}
                  role="tab"
                  tabIndex={tab === t ? 0 : -1}
                  aria-selected={tab === t}
                  aria-controls={TABS[t].panelId}
                  onClick={() => S.tab.set(t)}
                >
                  <Icon name={TABS[t].icon} />
                  {TABS[t].label}
                </button>
              ))}
            </div>
          </div>

          {tab === "studio" ? (
            <div id={TABS.studio.panelId} className="studio-body" role="tabpanel" aria-labelledby={TABS.studio.tabId}>
              {!ro && (
                <>
                  <div className="sec-h">
                    <span className="sec-t">Create</span>
                  </div>
                  <div className="tiles" role="group" aria-label="Create">
                    {OUTPUTS.map((k) => (
                      <button
                        key={k}
                        className={kindNow === k ? "tile on" : "tile"}
                        aria-pressed={kindNow === k}
                        disabled={nSrc === 0}
                        title={nSrc === 0 ? "Add a source first" : outputHint[k]}
                        onClick={() => {
                          S.chosen.set(S.chosen.get() === k ? null : k);
                          S.genErr.set("");
                        }}
                      >
                        <span className="tile-i">
                          <Icon name={outputIcon[k]} className="lg" />
                        </span>
                        <span className="tile-n">{outputLabel[k]}</span>
                        <span className="tile-d">{outputBlurb[k]}</span>
                      </button>
                    ))}
                  </div>
                  {nSrc === 0 && srcsLoaded && <p className="tiles-hint">Add a source first.</p>}

                  {kindNow !== null && nSrc > 0 && (
                    <Options
                      k={kindNow}
                      S={S}
                      cfg={cfg}
                      nSrc={nSrc}
                      language={language}
                      style={style}
                      audioFormat={audioFormat}
                      audioLength={audioLength}
                      audioFocus={audioFocus}
                      mmFocus={mmFocus}
                      ntFocus={ntFocus}
                      freshMap={freshMap}
                      freshNotes={freshNotes}
                      showCost={showCost}
                      overLimit={overLimit}
                      est={est}
                      estErr={estErr}
                      estLoading={estLoading}
                      quick={
                        kindNow === "mindmap"
                          ? [mmEstLoading, mmEst?.priced ? mmEst : null]
                          : [ntEstLoading, ntEst?.priced ? ntEst : null]
                      }
                      genErr={genErr}
                      generating={generating}
                      making={kindNow === "mindmap" ? mmMaking : kindNow === "notes" ? ntMaking : false}
                      onGenerate={A.generate}
                      onEstimate={() => {
                        setEstOpen(true);
                        if (est === null && !estLoading) void A.fetchEstimate();
                      }}
                      onOpen={onOpen}
                    />
                  )}
                </>
              )}

              <div className="sec-h out-h">
                <span className="sec-t">In this collection</span>
              </div>
              <div className="outs-list" aria-live="polite">
                {mmMaking && <PendingRow icon="diagram-3" text="Making a mind map…" />}
                {ntMaking && <PendingRow icon="journal-text" text="Writing study notes…" />}
                {mmErr !== "" && (
                  <ErrRow
                    icon="diagram-3"
                    title="The mind map could not be made"
                    detail={mmErr}
                    onDismiss={() => S.mm.err.set("")}
                  />
                )}
                {ntErr !== "" && (
                  <ErrRow
                    icon="journal-text"
                    title="The study notes could not be written"
                    detail={ntErr}
                    onDismiss={() => S.nt.err.set("")}
                  />
                )}
                {!loaded ? (
                  [0, 1].map((i) => (
                    <div key={i} className="out-row skel-row">
                      <div className="skel-line" />
                    </div>
                  ))
                ) : loadErr !== "" && made.length === 0 ? (
                  <ErrRow
                    title="What is in this collection could not be loaded"
                    detail={loadErr}
                    onRetry={A.reloadAll}
                  />
                ) : made.length === 0 && !mmMaking && !ntMaking ? (
                  <div className="outs-empty">
                    {ro
                      ? "Nothing was shared with it but its sources. Ask about them in the Ask tab."
                      : "Pick a format above. What you make appears here."}
                  </div>
                ) : null}
                {made.map((m) => {
                  if (m.kind === "session") {
                    const s = m.s;
                    return (
                      <SessionRow
                        key={madeKey(m)}
                        live={live.includes(s.sid)}
                        s={s}
                        onChanged={() => void A.load()}
                        onRename={ro ? undefined : (t) => A.rename({ kind: "session", id: s.sid }, t)}
                        onDelete={ro ? undefined : () => A.remove({ kind: "session", id: s.sid })}
                        err={rowErr[`session:${s.sid}`] ?? ""}
                        onDismissErr={() => A.setRowErr(`session:${s.sid}`, "")}
                      />
                    );
                  }
                  if (m.kind === "map") {
                    const x = m.m;
                    return (
                      <ItemRow
                        key={madeKey(m)}
                        icon="diagram-3"
                        what="mind map"
                        title={x.title}
                        facts={`${x.node_count} topics${x.focus === "" ? "" : ` · ${x.focus}`}`}
                        whenMs={x.created_ms}
                        on={open?.kind === "map" && open.id === x.id}
                        onOpen={() => onOpen({ kind: "map", id: x.id })}
                        onRename={ro ? undefined : (t) => A.rename({ kind: "map", id: x.id }, t)}
                        onDelete={ro ? undefined : () => A.remove({ kind: "map", id: x.id })}
                        err={rowErr[`map:${x.id}`] ?? ""}
                        onDismissErr={() => A.setRowErr(`map:${x.id}`, "")}
                      />
                    );
                  }
                  const x = m.n;
                  return (
                    <ItemRow
                      key={madeKey(m)}
                      icon="journal-text"
                      what="study notes"
                      title={x.title}
                      facts={`${counts(x.ideas, x.questions, x.terms)}${x.focus === "" ? "" : ` · ${x.focus}`}`}
                      whenMs={x.created_ms}
                      on={open?.kind === "notes" && open.id === x.id}
                      onOpen={() => onOpen({ kind: "notes", id: x.id })}
                      onRename={ro ? undefined : (t) => A.rename({ kind: "notes", id: x.id }, t)}
                      onDelete={ro ? undefined : () => A.remove({ kind: "notes", id: x.id })}
                      err={rowErr[`notes:${x.id}`] ?? ""}
                      onDismissErr={() => A.setRowErr(`notes:${x.id}`, "")}
                    />
                  );
                })}
              </div>
            </div>
          ) : (
            <AskTab st={chat} onSend={(t) => void A.sendChat(t)} />
          )}
        </section>

        {/* ── the viewer ── */}
        {open?.kind === "notes" && (
          <aside className="panel apps">
            <NotesView
              key={open.id}
              cid={cid}
              id={open.id}
              title={notes.find((n) => n.id === open.id)?.title ?? ""}
              onClose={closeViewer}
            />
          </aside>
        )}
        {open?.kind === "map" && (
          <aside className="panel apps">
            <MindMapView
              key={open.id}
              cid={cid}
              id={open.id}
              title={maps.find((m) => m.id === open.id)?.title ?? ""}
              onClose={closeViewer}
              onAsk={(q) => void A.askFromMap(q)}
            />
          </aside>
        )}
      </div>

      {cited && <SourceDrawer key={cited.name} cid={cid} opened={cited} onClose={() => setCited(null)} />}

      {sharing && (
        <ShareDialog
          cid={cid}
          title={summary?.title ?? ""}
          onClose={(changed) => {
            setSharing(false);
            if (changed) void A.load();
          }}
        />
      )}
      {estOpen && (
        <CostDialog
          est={est}
          audio={kindNow === "audio" ? audioDesc(audioFormat, audioLength) : null}
          loading={estLoading}
          err={estErr}
          onClose={() => setEstOpen(false)}
          onRetry={() => void A.fetchEstimate()}
          onBuild={() => {
            setEstOpen(false);
            const k = S.chosen.get();
            if (k !== null) A.generate(k);
          }}
        />
      )}
    </>
  );
}

/** The line under a read-only copy's header: what it is a copy of, a link to
 * that share while it is there, and why nothing here can be changed. */
function ReadOnlyNote({ id, origin }: { id: string; origin: string | null | undefined }) {
  const view: View = { kind: "shared", id };
  return (
    <p className="share-hint ro-note">
      <span className="ro-tag">Read only</span>
      Read-only copy of{" "}
      {origin ? (
        <a href={routeUrl(view)} onClick={(e) => follow(e, view)}>
          {readOnlyOf(origin)}
        </a>
      ) : (
        readOnlyOf(null)
      )}
      {READ_ONLY_TAIL}
    </p>
  );
}

/** The chosen tile's options, under the tiles: what to pick for it, what it
 * costs, and its one primary button. */
function Options({
  k,
  S,
  cfg,
  nSrc,
  language,
  style,
  audioFormat,
  audioLength,
  audioFocus,
  mmFocus,
  ntFocus,
  freshMap,
  freshNotes,
  showCost,
  overLimit,
  est,
  estErr,
  estLoading,
  quick,
  genErr,
  generating,
  making,
  onGenerate,
  onEstimate,
  onOpen,
}: {
  k: Output;
  S: PageState;
  cfg: Cfg;
  nSrc: number;
  language: string | null;
  style: string;
  audioFormat: string;
  audioLength: string;
  audioFocus: string;
  mmFocus: string;
  ntFocus: string;
  freshMap: string | null;
  freshNotes: string | null;
  showCost: boolean;
  overLimit: boolean;
  est: Estimate | null;
  estErr: string;
  estLoading: boolean;
  /** A map's or notes' estimate: whether it is on its way, and it, if priced. */
  quick: [boolean, { cost_usd: number; model: string; input_tokens: number } | null];
  genErr: string;
  generating: boolean;
  /** A map or notes of this kind is being made. */
  making: boolean;
  onGenerate: (k: Output) => void;
  onEstimate: () => void;
  onOpen: (o: Open) => void;
}) {
  const build = isBuild(k);
  const host = cfg.get(keys.SPEAKER1_NAME);
  const second = cfg.get(keys.SPEAKER2_NAME);
  const deck = k === "session" ? deckSummary(cfg, nSrc) : null;
  const offered = offeredLengths(audioFormat);
  const [qLoading, priced] = quick;
  return (
    <div className="opts" role="region" aria-label={`${outputLabel[k]} options`}>
      <div className="opts-h">
        <span className="opts-t">{outputLabel[k]}</span>
        <span className="opts-d">{outputHint[k]}</span>
      </div>
      {language !== null && (
        <p className="lang-chip">
          <Icon name="translate" />
          {build && !nativeVoices(cfg)
            ? `Writing in ${language} · voices are English. `
            : `Writing in ${language}. `}
          <SettingsLink tab="general" text="Settings › General" />
        </p>
      )}
      {k === "session" && (
        <>
          <div className="opt-l">Style</div>
          <div className="style-grid" role="radiogroup" aria-label="Style">
            {STYLES.map((st) => (
              <button
                key={st.id}
                className={style === st.id ? "style on" : "style"}
                role="radio"
                aria-checked={style === st.id}
                title={st.blurb}
                onClick={() => {
                  S.picked.set(true);
                  S.style.set(st.id);
                }}
              >
                <span className="sw" style={{ backgroundImage: `url(${thumbUrl(st.id)})` }} />
                <span className="style-n">{st.label}</span>
              </button>
            ))}
          </div>
          {deck !== null && (
            <p className="opt-hint">
              {`${deck} · `}
              <SettingsLink tab="defaults" text="Change defaults in Settings" />
            </p>
          )}
        </>
      )}
      {k === "audio" && (
        <>
          <div className="opt-l">Format</div>
          <div className="ao-formats" role="radiogroup" aria-label="Format">
            {AUDIO_FORMATS.map(([id, name, blurb]) => (
              <button
                key={id}
                className={audioFormat === id ? "ao-f on" : "ao-f"}
                role="radio"
                aria-checked={audioFormat === id}
                onClick={() => {
                  S.picked.set(true);
                  S.audioFormat.set(id);
                  fitLength(S);
                }}
              >
                <span className="ao-n">{name}</span>
                <span className="ao-d">{blurb}</span>
              </button>
            ))}
          </div>
          {offered.length > 0 && (
            <div className="ao-len" role="radiogroup" aria-label="Length">
              <span className="opt-l">Length</span>
              {offered.map((l) => (
                <button
                  key={l}
                  className={audioLength === l ? "chip on" : "chip"}
                  role="radio"
                  aria-checked={audioLength === l}
                  onClick={() => {
                    S.picked.set(true);
                    S.audioLength.set(l);
                  }}
                >
                  {l === "shorter" ? "Shorter" : l === "longer" ? "Longer" : "Default"}
                </button>
              ))}
            </div>
          )}
          {host !== undefined && second !== undefined && (
            <p className="opt-hint">
              {audioFormat === "brief"
                ? `Brief: ${host} alone, about 2 minutes. `
                : `Voices: ${host} and ${second}. `}
              <SettingsLink tab="voices" text="Change in Settings › Voices" />
            </p>
          )}
          <label className="opt-l" htmlFor="ao-focus">
            Focus
          </label>
          <input
            id="ao-focus"
            type="text"
            placeholder="A topic, an audience or a level (optional)"
            value={audioFocus}
            onChange={(e) => S.audioFocus.set(e.target.value)}
          />
        </>
      )}
      {k === "mindmap" && (
        <>
          <label className="opt-l" htmlFor="mm-focus">
            Focus
          </label>
          <input
            id="mm-focus"
            type="text"
            placeholder="Centre the map on a topic (optional)"
            value={mmFocus}
            onChange={(e) => S.mm.focus.set(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && onGenerate("mindmap")}
          />
          {freshMap !== null && (
            <p className="opt-note">
              <Icon name="check-circle-fill" />
              {" A map of exactly these sources exists. "}
              <button className="link-btn" onClick={() => onOpen({ kind: "map", id: freshMap })}>
                Open it
              </button>
            </p>
          )}
        </>
      )}
      {k === "notes" && (
        <>
          <label className="opt-l" htmlFor="nt-focus">
            Focus
          </label>
          <input
            id="nt-focus"
            type="text"
            placeholder="Centre the notes on a topic (optional)"
            value={ntFocus}
            onChange={(e) => S.nt.focus.set(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && onGenerate("notes")}
          />
          {freshNotes !== null && (
            <p className="opt-note">
              <Icon name="check-circle-fill" />
              {" Notes of exactly these sources exist. "}
              <button className="link-btn" onClick={() => onOpen({ kind: "notes", id: freshNotes })}>
                Open them
              </button>
            </p>
          )}
        </>
      )}

      {/* What it costs, said before the click. Off in Settings, it is not
          said at all; over the limit is said below either way. */}
      {build ? (
        showCost &&
        !overLimit && (
          <div className="est-banner" role="status" aria-label="Estimated cost">
            <span className="est-i" aria-hidden="true">
              <Icon name="info-circle" />
            </span>
            <div className="est-bt">
              {estLoading && est === null ? (
                <span className="dim">Working out the cost…</span>
              ) : est !== null ? (
                <>
                  <span>Estimated </span>
                  <strong aria-label={`${usdRangeSpoken(est.total_low_usd, est.total_high_usd)} US dollars`}>
                    {usdRange(est.total_low_usd, est.total_high_usd)}
                  </strong>
                  {anyUnpriced(est)
                    ? ", not counting a model with no price, so the real cost is unknown."
                    : est.limit_usd > 0
                      ? ` · within your ${usd(est.limit_usd)} limit.`
                      : "."}
                </>
              ) : estErr !== "" ? (
                <span className="dim">The cost could not be estimated.</span>
              ) : null}
            </div>
          </div>
        )
      ) : (
        <p className="opt-cost">
          {qLoading && priced === null ? (
            <span className="dim">Working out the cost…</span>
          ) : priced !== null ? (
            <span
              title={`${priced.model}, one call over about ${countShort(priced.input_tokens)} tokens of your sources`}
            >
              About <strong>{usd(priced.cost_usd)}</strong>
              {k === "mindmap" ? ", a few seconds." : ", under a minute."}
            </span>
          ) : null}
        </p>
      )}
      {/* Over the limit is said whether or not costs are shown: it is not a
          note about cost but the reason Generate is off. */}
      {build && est?.over_limit && <LimitNote e={est} audio={k === "audio"} className="opt-err" />}
      {genErr !== "" && (
        <div className="opt-err" role="alert">
          {genErr}
        </div>
      )}
      <div className="opts-a">
        {build && (
          <button title="Every step and what it costs, before you start" onClick={onEstimate}>
            Estimate cost
          </button>
        )}
        <span className="grow" />
        <button className="ghost" onClick={() => S.chosen.set(null)}>
          Cancel
        </button>
        <button
          className="primary"
          disabled={generating || (build && overLimit) || making}
          onClick={() => onGenerate(k)}
        >
          {generating ? "Starting…" : `Generate ${outputLabel[k].toLowerCase()}`}
        </button>
      </div>
    </div>
  );
}
