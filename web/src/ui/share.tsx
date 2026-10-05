// Sharing a collection with everyone on this studio, and reusing what others
// shared.
//
// A share puts one collection in the studio's public feed, Discover. The owner
// decides what it includes, per share: the sources or not, and each ready
// output. Reusing a share copies what it includes into a new collection of
// one's own, wholly independent of the original.
//
// There are no accounts on a studio yet, so "shared" is a boundary of what the
// pages show, not an access rule: everyone on the studio is the audience, and
// the owner is whoever opened the studio.

import { useCallback, useEffect, useRef, useState } from "react";
import { errText, getCollection } from "./api";
import { shareGet, shareRemove, shareSet, type FeedKind, type Share } from "./api-share";
import { mindmapList, notesList } from "./api-studio";
import { collTitle } from "./home";
import { Icon } from "./Icon";
import { notify, outputIcon, outputLabel, type Output } from "./shell";

/** The longest note a share carries: the server cuts it to this too. */
export const NOTE_MAX = 280;

/** The feed's two orders. */
export type FeedSort = "newest" | "reused";

export const FEED_SORTS: FeedSort[] = ["newest", "reused"];

export const feedSortLabel = (s: FeedSort) => (s === "newest" ? "Newest" : "Most reused");

/** What the feed lists: whole collections, or what is inside them, by kind. */
export const FEED_KINDS: FeedKind[] = ["all", "session", "audio", "mindmap", "notes"];

export const feedKindLabel: Record<FeedKind, string> = {
  all: "All",
  session: "Narrated slides",
  audio: "Audio overviews",
  mindmap: "Mind maps",
  notes: "Study notes",
};

/** What opening a shared output does, as its button says it: a deck plays, an
 * audio overview is listened to, a map or notes open to read. */
export function itemAction(k: Output): "Play" | "Listen" | "Open" {
  if (k === "session") return "Play";
  if (k === "audio") return "Listen";
  return "Open";
}

/** Items of one kind, the same identity however they were paged: a page asked
 * for after more was shared repeats what an earlier one held, and only the
 * first of each is kept. */
export function appendItems<T extends { share_id: string; key: string }>(have: T[], page: T[]): T[] {
  const seen = new Set(have.map((i) => `${i.share_id}/${i.key}`));
  const out = [...have];
  for (const i of page) {
    const id = `${i.share_id}/${i.key}`;
    if (seen.has(id)) continue;
    seen.add(id);
    out.push(i);
  }
  return out;
}

/** How often a share was reused, as its card says it. */
export function reusedLine(n: number): string {
  if (n <= 0) return "Not reused yet";
  if (n === 1) return "Reused once";
  return `Reused ${n} times`;
}

/** What a share lets people do with the copies they make, as a fact on its
 * card and its page. */
export const copyModeFact = (allowEdits: boolean) => (allowEdits ? "Editable copies" : "Read-only copies");

/** What Reuse will make, under the button on a shared collection's page. */
export const reuseHint = (allowEdits: boolean) =>
  allowEdits
    ? "Reusing makes a copy that is yours: what you change in it never touches the original."
    : "Reusing makes a copy of your own to read, ask and play. Its author did not allow edits, so it cannot be changed or shared again.";

/** What a read-only copy is a copy of, in its note: the original's title
 * while its share is there, else no name. */
export const readOnlyOf = (origin: string | null) => (origin === null ? "a shared collection" : `“${origin}”`);

/** Why a read-only copy has no way to change it, after what it is a copy of. */
export const READ_ONLY_TAIL = " — its author did not allow edits.";

/** The whole note a read-only copy shows under its header, in words. */
export const readOnlyLine = (origin: string | null) => `Read-only copy of ${readOnlyOf(origin)}${READ_ONLY_TAIL}`;

/** The key a share names an output by: `session:<sid>` for a deck or an audio
 * overview, `mindmap:<id>`, `notes:<id>`. */
export function outputKey(kind: Output, id: string): string {
  switch (kind) {
    case "session":
    case "audio":
      return `session:${id}`;
    case "mindmap":
      return `mindmap:${id}`;
    case "notes":
      return `notes:${id}`;
  }
}

/** What a shared output is, from the wire's kind: "session", "audio",
 * "mindmap", "notes". */
export function outputKind(wire: string): Output {
  if (wire === "audio" || wire === "mindmap" || wire === "notes") return wire;
  return "session";
}

/** One output that can be shared: ready, with its key, kind and name. */
type Shareable = { key: string; kind: Output; title: string; created_ms: number };

/** What a collection holds that a share can include, and its share if any. */
type Holding = { sources: number; outputs: Shareable[]; share: Share | null };

/** An output's name, or its kind while it has none. */
function named(title: string, kind: Output): string {
  return title.trim() === "" ? outputLabel[kind] : title;
}

/** Read what `cid` holds and its share. Only ready outputs can be shared: a
 * deck still preparing has nothing to show yet, and a failed one never will. */
async function holding(cid: string): Promise<Holding> {
  const g = await getCollection(cid);
  if (!g.found) throw new Error("This collection is no longer there.");
  const sources = g.collection?.sources ?? 0;
  const outputs: Shareable[] = g.outputs
    .filter((s) => s.state === "ready")
    .map((s) => {
      const kind: Output = s.kind === "audio" ? "audio" : "session";
      return { key: outputKey(kind, s.sid), kind, title: named(s.title, kind), created_ms: s.created_ms };
    });
  const [maps, notes, share] = await Promise.all([mindmapList(cid), notesList(cid), shareGet(cid)]);
  outputs.push(
    ...maps.map((m) => ({
      key: outputKey("mindmap", m.id),
      kind: "mindmap" as Output,
      title: named(m.title, "mindmap"),
      created_ms: m.created_ms,
    })),
    ...notes.map((n) => ({
      key: outputKey("notes", n.id),
      kind: "notes" as Output,
      title: named(n.title, "notes"),
      created_ms: n.created_ms,
    })),
  );
  outputs.sort((a, b) => b.created_ms - a.created_ms);
  return { sources, outputs, share };
}

/** Whether a share of this choice would hold anything: the server refuses an
 * empty one, so the dialog says so before it is asked. */
export function shareHasContent(includeSources: boolean, sources: number, picked: number): boolean {
  return (includeSources && sources > 0) || picked > 0;
}

/** Share a collection, or change or stop its share.
 *
 * What it holds is read when it opens, so the dialog is the same from the
 * collection's own page and from its card. A collection already shared opens
 * on what its share includes; one that is not opens with everything ticked,
 * for the owner to take away what should stay theirs.
 *
 * `onClose` says whether the share changed, so the page behind it can read it
 * again. */
export function ShareDialog({
  cid,
  title,
  onClose,
}: {
  cid: string;
  title: string;
  onClose: (changed: boolean) => void;
}) {
  const [held, setHeld] = useState<{ ok: Holding } | { err: string } | null>(null);
  const [withSources, setWithSources] = useState(true);
  const [picked, setPicked] = useState<string[]>([]);
  const [note, setNote] = useState("");
  // Off unless the owner turns it on: a copy is read-only by default.
  const [allowEdits, setAllowEdits] = useState(false);
  // What is on its way: a save, or a stop; the button that asked says so.
  const [busy, setBusy] = useState<"" | "save" | "stop">("");
  const [err, setErr] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => ref.current?.focus(), []);

  const load = useCallback(async () => {
    setHeld(null);
    try {
      const h = await holding(cid);
      if (h.share) {
        const s = h.share;
        setWithSources(s.include_sources && h.sources > 0);
        setPicked(s.outputs.filter((k) => h.outputs.some((o) => o.key === k)));
        setNote(s.note);
        setAllowEdits(s.allow_edits);
      } else {
        setWithSources(h.sources > 0);
        setPicked(h.outputs.map((o) => o.key));
      }
      setHeld({ ok: h });
    } catch (e) {
      setHeld({ err: errText(e) });
    }
  }, [cid]);
  useEffect(() => {
    // The dialog's state is set when the server answers.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  const h = held && "ok" in held ? held.ok : null;
  const sources = h?.sources ?? 0;
  const shared = !!h?.share;
  const hasContent = shareHasContent(withSources, sources, picked.length);
  const ready = h !== null;

  const submit = async () => {
    if (busy) return;
    setBusy("save");
    setErr("");
    try {
      await shareSet(cid, {
        include_sources: withSources,
        outputs: picked,
        note: note.trim(),
        allow_edits: allowEdits,
      });
      setBusy("");
      notify(shared ? "Share updated." : "Shared. Everyone on this studio can find it in Discover.");
      onClose(true);
    } catch (e) {
      setBusy("");
      setErr(`It could not be shared: ${errText(e)}`);
    }
  };
  const stop = async () => {
    if (busy || !h?.share) return;
    setBusy("stop");
    setErr("");
    try {
      await shareRemove(h.share.share_id);
      setBusy("");
      notify("Not shared any more. Copies people already made stay theirs.");
      onClose(true);
    } catch (e) {
      setBusy("");
      setErr(`It could not be unshared: ${errText(e)}`);
    }
  };

  const shown = collTitle(title);
  return (
    <>
      <div className="set-veil" onClick={() => onClose(false)} />
      <div
        ref={ref}
        className="set-dialog share-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="share-title"
        aria-describedby="share-hint"
        tabIndex={-1}
        onKeyDown={(e) => e.key === "Escape" && onClose(false)}
      >
        <div className="set-head share-head-bar">
          <h3 id="share-title">Share “{shown}”</h3>
          <button className="icon-btn" aria-label="Close" title="Close" onClick={() => onClose(false)}>
            <Icon name="x-lg" />
          </button>
        </div>
        <div className="share-body">
          <p id="share-hint" className="share-hint">
            Everyone on this studio can see what you include and reuse it as a copy. Your collection stays yours.
          </p>
          {held === null ? (
            <div className="set-note" role="status">
              <span className="mini-spin" /> Reading what this collection holds…
            </div>
          ) : "err" in held ? (
            <div className="set-note err" role="alert">
              What this collection holds could not be read: {held.err}
              <div>
                <button className="est-retry" onClick={() => void load()}>
                  <Icon name="arrow-clockwise" />
                  Try again
                </button>
              </div>
            </div>
          ) : (
            <>
              <div className="share-row">
                <div className="share-row-t">
                  <div id="share-src-l" className="set-label">
                    {held.ok.sources === 1 ? "Include sources (1)" : `Include sources (${held.ok.sources})`}
                  </div>
                  <div className="set-help">
                    {held.ok.sources === 0
                      ? "This collection has no sources yet."
                      : "Their text and links, so a copy can make new things from them."}
                  </div>
                </div>
                <button
                  className={withSources ? "switch on" : "switch"}
                  role="switch"
                  aria-labelledby="share-src-l"
                  aria-checked={withSources}
                  disabled={held.ok.sources === 0}
                  onClick={() => setWithSources((v) => !v)}
                >
                  <span className="knob" />
                </button>
              </div>
              <fieldset className="share-outs">
                <legend className="sec-t">Outputs</legend>
                {held.ok.outputs.length === 0 && (
                  <p className="set-help">
                    Nothing ready to share yet. Outputs still being made or failed cannot be shared.
                  </p>
                )}
                {held.ok.outputs.map((o) => (
                  <label key={o.key} className="share-out">
                    <input
                      type="checkbox"
                      checked={picked.includes(o.key)}
                      onChange={(e) => {
                        const on = e.target.checked;
                        setPicked((p) => [...p.filter((k) => k !== o.key), ...(on ? [o.key] : [])]);
                      }}
                    />
                    <span className="out-i">
                      <Icon name={outputIcon[o.kind]} />
                    </span>
                    <span className="share-out-t">
                      <span className="share-out-n">{o.title}</span>
                      <span className="share-out-k">{outputLabel[o.kind]}</span>
                    </span>
                  </label>
                ))}
              </fieldset>
              <div className="share-row">
                <div className="share-row-t">
                  <div id="share-edit-l" className="set-label">
                    Let people edit their copy and share it again
                  </div>
                  <div className="set-help">Off: a reused copy is read-only. On: it is theirs to change and publish.</div>
                </div>
                <button
                  className={allowEdits ? "switch on" : "switch"}
                  role="switch"
                  aria-labelledby="share-edit-l"
                  aria-checked={allowEdits}
                  onClick={() => setAllowEdits((v) => !v)}
                >
                  <span className="knob" />
                </button>
              </div>
              <div className="share-note">
                <label htmlFor="share-note">Note (optional)</label>
                <textarea
                  id="share-note"
                  maxLength={NOTE_MAX}
                  placeholder="What it is, who it is for"
                  value={note}
                  onChange={(e) => setNote([...e.target.value].slice(0, NOTE_MAX).join(""))}
                />
                <div className="share-count num">
                  {[...note].length}/{NOTE_MAX}
                </div>
              </div>
            </>
          )}
        </div>
        <div className="est-foot share-foot">
          {shared && (
            <button
              className="ghost bad"
              title="Take it out of Discover. Copies already made stay with whoever made them."
              disabled={busy !== ""}
              onClick={() => void stop()}
            >
              {busy === "stop" ? "Stopping…" : "Stop sharing"}
            </button>
          )}
          <span className="grow">
            {ready && !hasContent && (
              <span className="share-need" role="status">
                Include the sources or at least one output.
              </span>
            )}
            {err !== "" && (
              <span className="share-err" role="alert">
                {err}
              </span>
            )}
          </span>
          <div className="est-actions">
            <button className="ghost" onClick={() => onClose(false)}>
              Cancel
            </button>
            <button className="primary" disabled={!ready || !hasContent || busy !== ""} onClick={() => void submit()}>
              <Icon name="share" />
              {busy === "save" ? "Saving…" : shared ? "Update share" : "Share"}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
