// Study notes on the collection page: writing them, and the notes themselves
// in the viewer beside the Studio. A port of the old app's `notes.rs`.
//
// The notes are written and checked on the server; this file only reads them.
// Their `[n]` markers become the same chips the chat's cited answers use
// (`withChips`), so a citation looks and behaves the same wherever it
// appears. See `docs/study-notes-spec.md`.

import { useEffect, useState } from "react";
import { errText } from "./api";
import { sharedNotes } from "./api-share";
import {
  notesCreate,
  notesEstimate,
  notesGet,
  notesList,
  type QuickEstimate,
  type StudyNotes,
  type StudyNotesSummary,
} from "./api-studio";
import { Icon } from "./Icon";
import { citeGroups, mdToHtml, withChips, type Cite } from "./markdown";
import { covering, download, fileStem } from "./mindmap";
import { report } from "./shell";
import { store, type Store } from "./store";

// ── the state the page shares ────────────────────────────────────────────────

/** The collection page's study notes: the list, the options' focus, and notes
 * being written, shared by the Studio's options and its outputs list. */
export type NotesState = {
  notes: Store<StudyNotesSummary[]>;
  focus: Store<string>;
  making: Store<boolean>;
  err: Store<string>;
  est: Store<QuickEstimate | null>;
  estLoading: Store<boolean>;
};

export function newNotesState(): NotesState {
  return {
    notes: store<StudyNotesSummary[]>([]),
    focus: store(""),
    making: store(false),
    err: store(""),
    est: store<QuickEstimate | null>(null),
    estLoading: store(false),
  };
}

/** The collection's notes, newest first. */
export async function loadNotes(cid: string, st: NotesState): Promise<void> {
  try {
    st.notes.set(await notesList(cid));
  } catch (e) {
    report(`The study notes could not be loaded. ${errText(e)}`);
  }
}

/** What notes of the collection would cost. */
export async function estimateNotes(cid: string, st: NotesState): Promise<void> {
  st.estLoading.set(true);
  try {
    st.est.set(await notesEstimate(cid));
  } catch {
    st.est.set(null);
  }
  st.estLoading.set(false);
}

/** Write notes of the collection with the focus typed in, and return their id
 * to open. Null when it failed; the reason is in `st.err`. */
export async function makeNotes(cid: string, st: NotesState): Promise<string | null> {
  if (st.making.get()) return null;
  st.making.set(true);
  st.err.set("");
  const f = st.focus.get().trim();
  try {
    const n = await notesCreate(cid, f);
    const list = await notesList(cid);
    st.making.set(false);
    st.notes.set(list);
    st.focus.set("");
    return n.id;
  } catch (e) {
    st.making.set(false);
    st.err.set(errText(e));
    return null;
  }
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
    (shareId !== undefined ? sharedNotes(shareId, id) : notesGet(cid, id)).then(
      (n) => live && setNotes(n),
      (e) => live && setErr(errText(e)),
    );
    return () => {
      live = false;
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
