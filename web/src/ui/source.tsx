// A source, opened from a citation: its text in a drawer at the side of the
// page, the cited passage marked and scrolled into view. NotebookLM's
// citation click, and the same from the chat, a mind map's answers and study
// notes, since every chip is drawn by `markdown.ts`'s `withChips`. A port of
// the old app's `source.rs`.

import { useEffect, useState } from "react";
import { errText, sourceRead } from "./api";
import { Icon } from "./Icon";
import { mdToHtml } from "./markdown";
import { focusId } from "./shell";

/** A citation clicked: which source, and the passage it cites. */
export type Opened = { name: string; passage: string };

/** The source a citation opened, set whenever a chip on the page is clicked.
 *
 * The chips are HTML the app does not draw element by element, so one
 * listener on the document hears a click, or Enter, on any of them and passes
 * its source and passage over. It goes with the page that installed it. */
export function useCiteOpen(): [Opened | null, (o: Opened | null) => void] {
  const [opened, setOpened] = useState<Opened | null>(null);
  useEffect(() => {
    const pick = (e: Event) => {
      const t = e.target;
      return t instanceof Element ? (t.closest(".cite[data-src]") as HTMLElement | null) : null;
    };
    const go = (c: HTMLElement) => {
      const name = c.dataset.src ?? "";
      if (name !== "") setOpened({ name, passage: c.dataset.x ?? "" });
    };
    const onClick = (e: MouseEvent) => {
      const c = pick(e);
      if (!c) return;
      e.preventDefault();
      go(c);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const c = pick(e);
      if (!c) return;
      e.preventDefault();
      go(c);
    };
    document.addEventListener("click", onClick, true);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("click", onClick, true);
      document.removeEventListener("keydown", onKey, true);
    };
  }, []);
  return [opened, setOpened];
}

/** A source's paragraphs, each with whether the passage holds it.
 *
 * A passage is whole paragraphs of its source, trimmed and joined by blank
 * lines, so a paragraph is marked when it is one of the passage's. Should
 * none match exactly, which a source changed since the answer would do, the
 * paragraphs that hold the passage's opening words are marked instead, so the
 * reader still lands near it. */
export function markedParagraphs(text: string, passage: string): [string, boolean][] {
  const split = (s: string) =>
    s
      .split("\n\n")
      .map((p) => p.trim())
      .filter((p) => p !== "");
  const paras = split(text);
  const wanted = split(passage);
  let marks = paras.map((p) => wanted.includes(p));
  if (!marks.some((m) => m)) {
    const squash = (s: string) => s.split(/\s+/).filter((w) => w !== "").join(" ");
    const opening = [...squash(passage)].slice(0, 60).join("");
    if (opening !== "") marks = paras.map((p) => squash(p).includes(opening));
  }
  return paras.map((p, i) => [p, marks[i] ?? false]);
}

type Text = { title: string; url: string; text: string };

/** The drawer: the source's title and page, then its text with the passage
 * marked. Escape or the close button shuts it. Keyed on the source by its
 * caller, so another source starts from nothing. */
export function SourceDrawer({ cid, opened, onClose }: { cid: string; opened: Opened; onClose: () => void }) {
  const [got, setGot] = useState<{ ok: Text } | { err: string } | null>(null);
  const name = opened.name;
  useEffect(() => {
    let live = true;
    sourceRead(cid, name).then(
      (t) => live && setGot({ ok: { title: t.title, url: t.url, text: t.text } }),
      (e) => live && setGot({ err: errText(e) }),
    );
    return () => {
      live = false;
    };
  }, [cid, name]);
  // To the passage once the text is in, and again when another chip of the
  // same source names another passage.
  const passage = opened.passage;
  useEffect(() => {
    if (!got || !("ok" in got)) return;
    const t = setTimeout(() => document.querySelector(".srcv-body .mark")?.scrollIntoView({ block: "center" }), 0);
    return () => clearTimeout(t);
  }, [got, passage]);
  useEffect(() => focusId("srcv-close"), []);

  let body;
  if (got === null)
    body = (
      <div className="srcv-msg">
        <span className="mini-spin" />
        Reading the source…
      </div>
    );
  else if ("err" in got)
    body = (
      <div className="srcv-msg bad" role="alert">
        This source could not be opened. {got.err}
      </div>
    );
  else
    body = markedParagraphs(got.ok.text, passage).map(([p, mark], i) => (
      <div key={i} className={mark ? "md mark" : "md"} dangerouslySetInnerHTML={{ __html: mdToHtml(p) }} />
    ));
  const [title, url] = got && "ok" in got ? [got.ok.title, got.ok.url] : ["", ""];
  return (
    <aside className="srcv" role="dialog" aria-label="Source" onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <div className="srcv-head">
        <div className="srcv-t">
          <div className="srcv-n" title={title}>
            {title === "" ? "Source" : title}
          </div>
          {url !== "" && (
            <a className="srcv-u" href={url} target="_blank" rel="noopener noreferrer">
              <Icon name="link-45deg" />
              Open the page
            </a>
          )}
        </div>
        <button id="srcv-close" className="icon-btn" title="Close (Esc)" aria-label="Close the source" onClick={onClose}>
          <Icon name="x-lg" />
        </button>
      </div>
      <div className="srcv-body">{body}</div>
    </aside>
  );
}
