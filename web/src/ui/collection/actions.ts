// A collection page's work: loading, adding, making, renaming and deleting.
// Made once per page over its stores (`state.ts`); nothing here waits on a
// build. A deck or an audio overview prepares in the background for minutes,
// and the page follows the collection's event stream, where the server says
// what changed: its summary, its outputs and their progress, its sources,
// its mind maps and study notes, and how each one being made ended.

import {
  collectionRetitle,
  errText,
  followCollection,
  getCollection,
  isAbort,
  sleep,
  sourceAddFile,
  sourceAddText,
  sourceAddUrls,
  sourceRemove,
  type CollectionSummary,
  type SessionSummary,
} from "../api";
import {
  buildOutput,
  estimateOutput,
  followJob,
  mapSummaryOf,
  notesSummaryOf,
  researchTopic,
  studioOptions,
  type BuildReq,
  type Picks,
} from "../api-studio";
import { keepSame, sameOr } from "../helpers";
import { askSources, send, type ChatMade, type ChatState } from "../chat";
import { recheck } from "../making";
import { applyMaps, loadMaps, makeMap, mapEnded } from "../mindmap";
import { applyNotes, loadNotes, makeNotes, notesEnded } from "../notes";
import { deleteOutput, retitle, type Target } from "../outputs";
import type { Open } from "../routes";
import { isBuild, outputLabel, report, type Output } from "../shell";
import { FETCHING, serverSources, srcFrom, srcOfServer, type Src } from "../sources";
import { applyDefaults, failedSrc, sameOpen, staged, type PageState } from "./state";
import { uploadProblem } from "./upload";

/** How often a page whose event stream is down reads its collection again. */
export const FALLBACK_MS = 30_000;

/** The page's work: loading, adding, making, renaming and deleting. Made once
 * per page, over its stores. */
export function pageActions(cid: string, S: PageState, chat: ChatState) {
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
  // Which read of the sources is the latest, as `loadGen` is the collection's.
  let srcGen = 0;
  // Which read of the Create panel's options is the latest.
  let optsGen = 0;
  // Cancels what the page asked for when it goes: reads, waits and the
  // estimate. A change already sent (an add, a rename) is left to finish.
  let life = new AbortController();
  const signal = () => life.signal;
  // A row not on the server (being read, failed) has no stored name to know
  // it by, so it is given a key of its own for as long as it is shown.
  const localKey = () => `local-${++rowN}`;
  const pushSrc = (s: Src) => S.srcs.set((v) => [...v, s.key === undefined ? { ...s, key: localKey() } : s]);

  // The collection as the server says it is now, from a read or its stream.
  const applyCollection = (c: CollectionSummary) => {
    if (!S.typing.get()) S.title.set(c.title);
    props().setCrumb(c.title);
    S.summary.set((was) => sameOr(was, c));
  };
  // The rows that did not change stay the objects they were, so an update
  // redraws only what moved.
  const applyOutputs = (outputs: SessionSummary[]) => {
    S.outputs.set((was) => keepSame(was, outputs, (o) => o.sid));
    S.loadErr.set("");
    S.loaded.set(true);
  };

  const load = async () => {
    const my = ++loadGen;
    let got: Awaited<ReturnType<typeof getCollection>> | null = null;
    let err = "";
    try {
      got = await getCollection(cid, signal());
    } catch (e) {
      if (isAbort(e)) return;
      err = errText(e);
    }
    if (loadGen !== my) return;
    if (got?.found) {
      if (got.collection) applyCollection(got.collection);
      applyOutputs(got.outputs);
    } else if (got) S.missing.set(true);
    else S.loadErr.set(err);
    S.loaded.set(true);
  };

  // The server's list, with the rows it does not have kept after it: a page
  // still being read, or one that failed and says why.
  const applySources = (list: Src[]) => {
    const local = S.srcs.get().filter((s) => !s.ok || s.detail === FETCHING);
    const next = list.map((s) => {
      const i = icons.get(s.url);
      return i ? { ...s, icon: i } : s;
    });
    S.srcs.set([...next, ...local]);
    S.srcsErr.set("");
    S.srcsLoaded.set(true);
  };

  const loadSources = async () => {
    const my = ++srcGen;
    try {
      const list = await serverSources(cid, signal());
      if (srcGen !== my) return;
      applySources(list);
    } catch (e) {
      if (isAbort(e) || srcGen !== my) return;
      S.srcsErr.set(errText(e));
      S.srcsLoaded.set(true);
    }
  };

  // Ask how the maps and notes this page waits on ended, when the stream
  // may have missed saying it.
  const recheckMade = () => Promise.all([recheck(S.mm.mk, signal()), recheck(S.nt.mk, signal())]);

  // Follow the collection on the server's event stream: its summary, its
  // decks and audio overviews with their progress, its sources, and its maps
  // and notes, as they change. Only while the stream is down is the collection read again, and
  // then slowly. Returns what stops it.
  const follow = () => {
    let up = true;
    const stop = new AbortController();
    const close = followCollection(cid, {
      // What the stream says is newer than any read already on its way.
      collection: (c) => {
        ++loadGen;
        applyCollection(c);
      },
      outputs: (o) => {
        ++loadGen;
        applyOutputs(o);
      },
      progress: (p) => S.progress.set((m) => ({ ...m, [p.session_id]: p })),
      sources: (list) => {
        ++srcGen;
        applySources(list.map(srcOfServer));
      },
      mindmaps: (list) => applyMaps(S.mm, list.map(mapSummaryOf)),
      notes: (list) => applyNotes(S.nt, list.map(notesSummaryOf)),
      // Which of the two the job made is told by whose wait or list has it.
      ended: ({ job_id, error }) => {
        mapEnded(S.mm, job_id, error);
        notesEnded(S.nt, job_id, error);
      },
      gone: () => S.missing.set(true),
      up: (ok) => {
        // Back after a drop: a job that ended meanwhile was never said.
        if (ok && !up) void recheckMade();
        up = ok;
      },
    });
    void (async () => {
      while (!stop.signal.aborted) {
        await sleep(FALLBACK_MS, stop.signal);
        if (stop.signal.aborted || up || S.missing.get()) continue;
        await load();
        await loadSources();
        await loadMaps(cid, S.mm, signal());
        await loadNotes(cid, S.nt, signal());
        await recheckMade();
      }
    })();
    return () => {
      stop.abort();
      close();
    };
  };

  // What the Create panel offers, read again when the settings or the
  // sources change: the server words it from both.
  const loadOptions = async () => {
    const my = ++optsGen;
    try {
      const o = await studioOptions(cid, signal());
      if (optsGen !== my) return;
      S.opts.set(o);
      S.optsErr.set("");
      applyDefaults(S);
    } catch (e) {
      if (isAbort(e) || optsGen !== my) return;
      S.optsErr.set(errText(e));
    }
  };

  const reloadAll = () => {
    void load();
    void loadMaps(cid, S.mm, signal());
    void loadNotes(cid, S.nt, signal());
  };

  // One input for both kinds of source: anything that looks like a link is
  // fetched, everything else is kept as a note.
  const addSource = async () => {
    const raw = S.draft.get().trim();
    if (raw === "" || S.adding.get()) return;
    S.adding.set(true);
    const all = raw.split(/\s+/).filter((t) => t.startsWith("http://") || t.startsWith("https://"));
    // Past the most one Add takes, as the server says it, the rest wait in
    // the box. Before it has said, all go, and it says if that is too many.
    const most = S.opts.get()?.upload.max_links ?? all.length;
    const urls = all.slice(0, most);
    S.draft.set(all.slice(most).join("\n"));
    if (urls.length > 0) {
      const rows: Src[] = urls.map((u) => ({ icon: "", name: u, detail: FETCHING, ok: true, url: u, file: "", key: localKey() }));
      const mine = new Set(rows.map((r) => r.key));
      const drop = () => S.srcs.set((v) => v.filter((s) => !mine.has(s.key)));
      S.srcs.set((v) => [...v, ...rows]);
      try {
        const got = await sourceAddUrls(cid, urls);
        drop();
        // A page that did not arrive has to be visible: a deck built without
        // it looks exactly like a deck built with it. The ones that did are
        // read back from the server below.
        for (const g of got) {
          const s = srcFrom(g);
          if (!s.ok) pushSrc(s);
          else if (s.icon !== "") icons.set(s.url, s.icon);
        }
      } catch (e) {
        drop();
        pushSrc(failedSrc("Those links could not be read", errText(e)));
      }
    } else {
      const name = raw.split(/\s+/).slice(0, 6).join(" ");
      S.addingNote.set(name === "" ? "Note" : name);
      try {
        const r = await sourceAddText(cid, raw, name);
        if (!r.ok) throw new Error(r.error);
      } catch (e) {
        pushSrc(failedSrc(name === "" ? "Note" : name, errText(e)));
      }
    }
    // The list first, then the pending row out: no gap where the note is
    // neither being added nor listed.
    await loadSources();
    S.addingNote.set("");
    S.adding.set(false);
    await load();
  };

  // Research a topic: the add box's text as a brief, read across the web for
  // about a minute and added as one written report.
  const research = async () => {
    const topic = S.draft.get().trim();
    if (topic === "") return;
    S.draft.set("");
    const n = ++rowN;
    S.researching.set((v) => [...v, [n, topic, ""]]);
    let why: string | null;
    try {
      // The server's job says how far it is and, when it fails, why: the row
      // follows it rather than waiting for a source to appear.
      const job = await researchTopic(cid, topic);
      why = await followJob(
        job.id,
        (said) => S.researching.set((v) => v.map((r) => (r[0] === n ? [n, topic, said] : r))),
        signal(),
      );
    } catch (e) {
      why = errText(e);
    }
    S.researching.set((v) => v.filter((r) => r[0] !== n));
    if (why !== null) pushSrc(failedSrc(`Research: ${topic}`, why));
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
      // By its own key: two rows that failed the same way are still two.
      S.srcs.set((v) => v.filter((x) => (s.key === undefined ? x !== s : x.key !== s.key)));
      return;
    }
    if (S.removing.get().includes(s.file)) return;
    S.removing.set((v) => [...v, s.file]);
    void (async () => {
      try {
        await sourceRemove(cid, s.file);
        setRowErr(`src:${s.file}`, "");
      } catch (e) {
        setRowErr(`src:${s.file}`, `It could not be removed. ${errText(e)}`);
      }
      await loadSources();
      S.removing.set((v) => v.filter((f) => f !== s.file));
      await load();
    })();
  };

  // Files picked or dropped: refused at once when the studio could not read
  // them, otherwise sent one at a time, each row becoming its source as it
  // lands. The collection is looked at again after, for the name.
  const uploadFiles = async (files: File[]) => {
    const queue: [number, File][] = [];
    for (const f of files) {
      const why = uploadProblem(f.name, f.size, S.opts.get()?.upload ?? null);
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

  // What is being done to one row, set or cleared (empty clears it).
  const setRowBusy = (key: string, text: string) =>
    S.rowBusy.set((m) => {
      const next = { ...m };
      if (text === "") delete next[key];
      else next[key] = text;
      return next;
    });

  // A deck, an audio overview, a map or notes: renamed in place, in the list
  // and in the viewer if it is open, without reading anything back.
  const rename = (t: Target, next: string) => {
    const key = `${t.kind}:${t.id}`;
    if (S.rowBusy.get()[key]) return;
    setRowBusy(key, "Renaming…");
    void (async () => {
      try {
        await retitle(cid, t, next);
        setRowErr(key, "");
      } catch (e) {
        setRowErr(key, `It could not be renamed. ${errText(e)}`);
        setRowBusy(key, "");
        return;
      }
      // A read already on its way set out before the rename and would put the
      // old name back: it is dropped, and the list read again behind the new
      // name shown at once.
      if (t.kind === "session") {
        ++loadGen;
        S.outputs.set((v) => v.map((s) => (s.sid === t.id ? { ...s, title: next } : s)));
        void load();
      } else if (t.kind === "map") {
        S.mm.maps.set((v) => v.map((m) => (m.id === t.id ? { ...m, title: next } : m)));
        void loadMaps(cid, S.mm, signal());
      } else {
        S.nt.notes.set((v) => v.map((n) => (n.id === t.id ? { ...n, title: next } : n)));
        void loadNotes(cid, S.nt, signal());
      }
      setRowBusy(key, "");
    })();
  };

  // Any output, deleted: closed first if it is the one open, then the lists
  // read back.
  const remove = (t: Target) => {
    const key = `${t.kind}:${t.id}`;
    if (S.rowBusy.get()[key]) return;
    const shown: Open | null = t.kind === "session" ? null : { kind: t.kind, id: t.id };
    if (shown && sameOpen(props().open, shown)) props().onOpen(null);
    setRowBusy(key, "Deleting…");
    void (async () => {
      try {
        await deleteOutput(cid, t);
        setRowErr(key, "");
      } catch (e) {
        setRowErr(key, `It could not be deleted. ${errText(e)}`);
      }
      if (t.kind === "map") await loadMaps(cid, S.mm, signal());
      else if (t.kind === "notes") await loadNotes(cid, S.nt, signal());
      await load();
      setRowBusy(key, "");
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
    S.renaming.set(true);
    void (async () => {
      try {
        await collectionRetitle(cid, t);
      } catch (e) {
        report(`The collection could not be renamed. ${errText(e)}`);
      }
      await load();
      S.renaming.set(false);
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
    const speakers = S.speakers.get();
    return {
      kind: "slides",
      ...(title === null ? {} : { title }),
      style: S.style.get(),
      // Left out, the server reads the count from the settings.
      ...(speakers === null ? {} : { speakers }),
    };
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
      const e = await estimateOutput(cid, buildReq(kind, null), signal());
      // A newer request is out; its reply is the one to show.
      if (estSeq !== my) return;
      S.est.set(e);
    } catch (e) {
      if (isAbort(e) || estSeq !== my) return;
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
      if (m.kind === "mindmap") await loadMaps(cid, S.mm, signal());
      else await loadNotes(cid, S.nt, signal());
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
    // Mounted again after a dispose (React does so once in development).
    if (life.signal.aborted) life = new AbortController();
    // Not the last collection's name while this one's is on its way.
    props().setCrumb("");
    void load();
    void loadSources();
    void loadMaps(cid, S.mm, signal());
    void loadNotes(cid, S.nt, signal());
  };
  const dispose = () => life.abort();

  return {
    load,
    loadSources,
    loadOptions,
    follow,
    reloadAll,
    signal,
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

export type PageActions = ReturnType<typeof pageActions>;
