// Study notes on the collection page: writing them, and the notes themselves
// in the viewer beside the Studio. A port of the old app's `notes.rs`.
//
// The notes are written and checked on the server; this file only reads them.
// Their `[n]` markers become the same chips the chat's cited answers use
// (`withChips`), so a citation looks and behaves the same wherever it
// appears. See `docs/study-notes-spec.md`.

import { useEffect, useState } from "react";
import { errText, isAbort } from "./api";
import { sharedNotes } from "./api-share";
import {
  notesCreate,
  notesEstimate,
  notesGet,
  notesList,
  type StudyNotes,
  type StudyNotesSummary,
} from "./api-studio";
import type { Estimate } from "./dialogs";
import { Icon } from "./Icon";
import { citeGroups, type Cite } from "./cite";
import { mdToHtml, withChips } from "./markdown";
import { download, fileStem } from "./common";
import { jobEnded, madeBy, newMaking, sorted, type MakingState } from "./making";
import { covering } from "./mindmap";
import { keepSame } from "./helpers";
import { store, type Store } from "./store";

// ── the state the page shares ────────────────────────────────────────────────

/** The collection page's study notes: the list, the options' focus, and notes
 * being written, shared by the Studio's options and its outputs list. */
export type NotesState = {
  notes: Store<StudyNotesSummary[]>;
  focus: Store<string>;
  making: Store<boolean>;
  err: Store<string>;
  /** The list has answered once, well or not. */
  loaded: Store<boolean>;
  /** Why the list could not be read; said in the outputs list. */
  loadErr: Store<string>;
  est: Store<Estimate | null>;
  estLoading: Store<boolean>;
  /** Why the last estimate could not be had, as a sentence. */
  estErr: Store<string>;
  /** Which list read and which estimate are the latest: an older answer
   * landing after a newer one is dropped. */
  seq: { load: number; est: number };
  /** The notes being written, followed on the server. */
  mk: MakingState;
};

export function newNotesState(): NotesState {
  return {
    notes: store<StudyNotesSummary[]>([]),
    focus: store(""),
    making: store(false),
    err: store(""),
    loaded: store(false),
    loadErr: store(""),
    est: store<Estimate | null>(null),
    estLoading: store(false),
    estErr: store(""),
    seq: { load: 0, est: 0 },
    mk: newMaking(),
  };
}

/** The collection's notes, newest first. Notes being written are not
 * listed: the list says "Writing study notes…" while the server writes
 * them, and the collection's stream brings the notes again once they are
 * done. */
export async function loadNotes(cid: string, st: NotesState, signal?: AbortSignal): Promise<void> {
  const my = ++st.seq.load;
  try {
    const list = await notesList(cid, signal);
    if (my !== st.seq.load) return;
    applyNotes(st, list);
  } catch (e) {
    if (isAbort(e) || my !== st.seq.load) return;
    st.loadErr.set(errText(e));
    // What the server is making is not known now; only this page's own asks.
    st.making.set(st.mk.asked > 0);
  }
  st.loaded.set(true);
}

/** The notes as the server listed them, by a read or on the stream. */
export function applyNotes(st: NotesState, list: StudyNotesSummary[]): void {
  // Newer than any read already on its way.
  ++st.seq.load;
  const { ready, making } = sorted(list, st.mk);
  st.notes.set((was) => keepSame(was, ready, (x) => x.id));
  st.making.set(making);
  st.loadErr.set("");
  st.loaded.set(true);
}

/** The stream said a notes job ended: a failure nobody here asked for is
 * said all the same. */
export function notesEnded(st: NotesState, job: string, why: string | null): void {
  const shown = jobEnded(st.mk, job, why);
  if (shown !== null) st.err.set(shown);
}

/** What notes of the collection would cost. */
export async function estimateNotes(cid: string, st: NotesState, signal?: AbortSignal): Promise<void> {
  const my = ++st.seq.est;
  st.estLoading.set(true);
  st.estErr.set("");
  try {
    const e = await notesEstimate(cid, signal);
    if (my !== st.seq.est) return;
    st.est.set(e);
  } catch (e) {
    if (isAbort(e) || my !== st.seq.est) return;
    st.est.set(null);
    st.estErr.set(errText(e));
  }
  st.estLoading.set(false);
}

/** Write notes of the collection with the focus typed in, and return their
 * id to open once the server has written them. Null when it failed; the
 * reason is in `st.err`. */
export async function makeNotes(cid: string, st: NotesState): Promise<string | null> {
  if (st.making.get()) return null;
  st.mk.asked++;
  st.making.set(true);
  st.err.set("");
  const f = st.focus.get().trim();
  let out: { id: string } | { why: string };
  try {
    out = await madeBy(st.mk, async () => {
      const started = await notesCreate(cid, f);
      st.focus.set("");
      return started;
    });
  } catch (e) {
    out = { why: errText(e) };
  }
  st.mk.asked--;
  // Read back as the newest list: it says whether anything is still being made.
  await loadNotes(cid, st);
  st.loaded.set(true);
  if ("why" in out) {
    st.err.set(out.why);
    return null;
  }
  return out.id;
}

/** The newest notes made from exactly `sources` with no focus: the ones that
 * are already up to date, so writing more would say the same again. */
export function coveringNotes(notes: StudyNotesSummary[], sources: string[]): string | null {
  return covering(notes, sources);
}

/** "6 ideas · 10 questions · 18 terms", leaving out what is not there. */
export function counts(ideas: number, questions: number, terms: number): string {
  const part = (n: number, one: string, many: string) => (n === 0 ? null : n === 1 ? `1 ${one}` : `${n} ${many}`);
  return [part(ideas, "idea", "ideas"), part(questions, "question", "questions"), part(terms, "term", "terms")]
    .filter((p) => p !== null)
    .join(" · ");
}

// ── the notes ────────────────────────────────────────────────────────────────

/** Markdown with `[n]` markers as HTML with citation chips. */
export function cited(src: string, cites: Cite[]): string {
  return withChips(mdToHtml(src), cites);
}

/** The same, for a line that is not a paragraph (a question, a definition):
 * the `<p>` the Markdown renderer wraps it in is taken off, so it sits inline. */
export function citedInline(src: string, cites: Cite[]): string {
  const html = cited(src, cites);
  const t = html.trim();
  if (t.startsWith("<p>") && t.endsWith("</p>")) {
    const inner = t.slice(3, -4);
    if (!inner.includes("<p>")) return inner;
  }
  return html;
}

/** Flip a citation's popover below its chip when there is no room above it.
 *
 * The popover opens above the chip, and a chip near the top of a scrolling
 * pane had it cut off by the pane's edge: measured in the notes, a popover at
 * 4 px against a pane starting at 108 px. It is also kept inside its pane
 * sideways, narrowed when the pane is narrow, since a chip near an edge
 * opened its card half outside. Which way it opens is decided at the moment
 * it opens, from where the chip is, for every chip on the page — the chat's
 * included. Installed once. */
let citeFlip = false;
export function installCiteFlip(): void {
  if (citeFlip) return;
  citeFlip = true;
  const f = (e: Event) => {
    const t = e.target;
    const c = t instanceof Element ? (t.closest(".cite") as HTMLElement | null) : null;
    if (!c) return;
    let top = 0;
    for (let p = c.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowY;
      if (o === "auto" || o === "scroll") {
        top = p.getBoundingClientRect().top;
        break;
      }
    }
    const r = c.getBoundingClientRect();
    c.toggleAttribute("data-below", r.top - top < 220);
    let L = 8;
    let R = innerWidth - 8;
    for (let p = c.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if (o !== "visible") {
        const b = p.getBoundingClientRect();
        L = Math.max(L, b.left + 8);
        R = Math.min(R, b.right - 8);
        break;
      }
    }
    const w = Math.max(160, Math.min(320, R - L));
    const mid = r.left + r.width / 2;
    const x = Math.min(Math.max(mid - w / 2, L), R - w);
    c.style.setProperty("--pop-w", `${w}px`);
    c.style.setProperty("--pop-x", `${x - (mid - w / 2)}px`);
  };
  document.addEventListener("mouseover", f, true);
  document.addEventListener("focusin", f, true);
}

/** The notes open in the viewer: read top to bottom, with the quiz's answers
 * hidden until asked for, the way a study guide is used. Keyed on the notes
 * by its caller, so other notes start from nothing. */
export function NotesView({
  cid,
  id,
  title,
  onClose,
  readOnly = false,
  shareId,
}: {
  cid: string;
  id: string;
  /** The share they are read through, on a shared collection's page. */
  shareId?: string;
  /** Their name as the outputs list has it, which a rename changes while
   * they are open. Empty until the list has loaded. */
  title: string;
  onClose: () => void;
  /** Someone else's notes, on a shared collection's page: said in the bar. */
  readOnly?: boolean;
}) {
  const [notes, setNotes] = useState<StudyNotes | null>(null);
  const [err, setErr] = useState("");
  // Which answers are showing, by question index.
  const [shown, setShown] = useState<Set<number>>(() => new Set());

  useEffect(installCiteFlip, []);
  useEffect(() => {
    let live = true;
    const ctrl = new AbortController();
    (shareId !== undefined ? sharedNotes(shareId, id, ctrl.signal) : notesGet(cid, id, ctrl.signal)).then(
      (n) => live && setNotes(n),
      (e) => live && !isAbort(e) && setErr(errText(e)),
    );
    return () => {
      live = false;
      ctrl.abort();
    };
  }, [cid, id, shareId]);

  const n = notes;
  // The list's name for them first, as the map's viewer does.
  const heading = title.trim() === "" ? (n?.title ?? "") : title;
  const cites = n?.citations ?? [];
  // What the person should know about how these were made, in one line.
  let note: string | null = null;
  if (n) {
    const parts = [n.citations.length === 1 ? "1 passage cited" : `${n.citations.length} passages cited`];
    if (n.dropped > 0)
      parts.push(`${n.dropped} citation${n.dropped === 1 ? "" : "s"} removed: the passage did not say it`);
    if (n.unchecked) parts.push("citations not checked: the notes are not in English");
    if (n.excerpted) parts.push("written from excerpts of long sources");
    if (n.focus !== "") parts.push(`focus: ${n.focus}`);
    note = parts.join(" · ");
  }
  const allShown = !!n && n.quiz.length > 0 && shown.size === n.quiz.length;
  const show = (i: number, on: boolean) =>
    setShown((s) => {
      const next = new Set(s);
      if (on) next.add(i);
      else next.delete(i);
      return next;
    });

  return (
    <div className="mm-view notes-view">
      <div className="mm-bar">
        <div className="mm-title">
          <div className="mm-h">{heading}</div>
          {note !== null && (
            <div className="mm-note">
              {readOnly && <span className="ro-tag">Read only</span>}
              {note}
            </div>
          )}
        </div>
        <div className="mm-tools" role="toolbar" aria-label="Study notes">
          <button
            className="icon-btn"
            title="Download as Markdown"
            aria-label="Download as Markdown"
            onClick={() => n && download(`${fileStem(heading)}.md`, "text/markdown", n.markdown)}
          >
            <Icon name="download" />
          </button>
          <button className="icon-btn" title="Close the notes" aria-label="Close the notes" onClick={onClose}>
            <Icon name="x-lg" />
          </button>
        </div>
      </div>
      <div className="notes-body">
        {err !== "" ? (
          <div className="mm-msg bad" role="alert">
            {err}
          </div>
        ) : n ? (
          <article className="notes md">
            {n.overview !== "" && (
              <section className="notes-sec">
                <h2>Overview</h2>
                <div dangerouslySetInnerHTML={{ __html: cited(n.overview, cites) }} />
              </section>
            )}
            {n.ideas.length > 0 && (
              <section className="notes-sec">
                <h2>Key ideas</h2>
                {n.ideas.map((idea, i) => (
                  <div key={i} className="notes-idea">
                    <h3>{idea.heading}</h3>
                    <div dangerouslySetInnerHTML={{ __html: cited(idea.body, cites) }} />
                  </div>
                ))}
              </section>
            )}
            {n.quiz.length > 0 && (
              <section className="notes-sec">
                <div className="notes-sec-h">
                  <h2>Quiz</h2>
                  <button
                    className="chip-btn"
                    onClick={() => setShown(allShown ? new Set() : new Set(n.quiz.map((_, i) => i)))}
                  >
                    {allShown ? "Hide all answers" : "Show all answers"}
                  </button>
                </div>
                <p className="dim small">Answer each in two or three sentences, then check.</p>
                <ol className="notes-quiz">
                  {n.quiz.map((q, i) => (
                    <li key={i}>
                      <div className="notes-q" dangerouslySetInnerHTML={{ __html: citedInline(q.question, cites) }} />
                      {q.answer === "" ? (
                        <div className="dim small">The sources gave no answer to check this one against.</div>
                      ) : shown.has(i) ? (
                        <div className="notes-a">
                          <div dangerouslySetInnerHTML={{ __html: cited(q.answer, cites) }} />
                          <button className="link-btn" onClick={() => show(i, false)}>
                            Hide answer
                          </button>
                        </div>
                      ) : (
                        <button className="link-btn" aria-expanded="false" onClick={() => show(i, true)}>
                          Show answer
                        </button>
                      )}
                    </li>
                  ))}
                </ol>
              </section>
            )}
            {n.essays.length > 0 && (
              <section className="notes-sec">
                <h2>Essay questions</h2>
                <p className="dim small">No answers: each one asks you to connect several of the ideas above.</p>
                <ol>
                  {n.essays.map((e, i) => (
                    <li key={i} dangerouslySetInnerHTML={{ __html: citedInline(e, cites) }} />
                  ))}
                </ol>
              </section>
            )}
            {n.glossary.length > 0 && (
              <section className="notes-sec">
                <h2>Glossary</h2>
                <dl className="notes-gloss">
                  {n.glossary.map((t, i) => (
                    <div key={i}>
                      <dt>{t.term}</dt>
                      <dd dangerouslySetInnerHTML={{ __html: citedInline(t.definition, cites) }} />
                    </div>
                  ))}
                </dl>
              </section>
            )}
            {cites.length > 0 && (
              <section className="notes-sec notes-src">
                <h2>Sources</h2>
                {citeGroups(cites).map(([t, url, ns], i) => (
                  <div key={i} className="cites">
                    {url === "" ? (
                      t
                    ) : (
                      <a href={url} target="_blank" rel="noopener noreferrer">
                        {t}
                      </a>
                    )}
                    <span className="cite-ns"> passages {ns}</span>
                  </div>
                ))}
              </section>
            )}
          </article>
        ) : (
          <div className="mm-msg">
            <span className="mini-spin" /> Opening the notes…
          </div>
        )}
      </div>
    </div>
  );
}
