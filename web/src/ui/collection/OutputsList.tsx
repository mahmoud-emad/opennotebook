// The Studio's lower half: everything already made from these sources, newest
// first and every kind mixed, each saying where it is: preparing with its
// live step, failed with the reason and a retry, or ready to open.

import { memo, useMemo } from "react";
import type { OutputProgress, SessionSummary } from "../api";
import type { MindMapSummary, StudyNotesSummary } from "../api-studio";
import { counts } from "../notes";
import {
  ErrRow,
  ItemRow,
  PendingRow,
  SessionRow,
  madeCreated,
  madeKey,
  type Made,
} from "../outputs";
import type { Open } from "../routes";
import { useStore } from "../store";
import { VideoLine } from "../video";
import type { PageActions } from "./actions";
import type { PageState } from "./state";

export function OutputsList({
  S,
  A,
  ro,
  open,
  onOpen,
}: {
  S: PageState;
  A: PageActions;
  ro: boolean;
  open: Open | null;
  onOpen: (o: Open) => void;
}) {
  const outputs = useStore(S.outputs);
  const maps = useStore(S.mm.maps);
  const notes = useStore(S.nt.notes);
  const loaded = useStore(S.loaded);
  const loadErr = useStore(S.loadErr);
  const mmMaking = useStore(S.mm.making);
  const ntMaking = useStore(S.nt.making);
  const mmErr = useStore(S.mm.err);
  const ntErr = useStore(S.nt.err);
  const mmLoaded = useStore(S.mm.loaded);
  const ntLoaded = useStore(S.nt.loaded);
  const mmLoadErr = useStore(S.mm.loadErr);
  const ntLoadErr = useStore(S.nt.loadErr);
  // Every kind has answered once: until then the rows would arrive kind by
  // kind and reorder under the person's eyes.
  const ready = loaded && mmLoaded && ntLoaded;
  const rowErr = useStore(S.rowErr);
  const rowBusy = useStore(S.rowBusy);

  // Everything made, newest first, every kind in one list.
  const made = useMemo(
    () =>
      [
        ...outputs.map((s): Made => ({ kind: "session", s })),
        ...maps.map((m): Made => ({ kind: "map", m })),
        ...notes.map((n): Made => ({ kind: "notes", n })),
      ].sort((a, b) => madeCreated(b) - madeCreated(a)),
    [outputs, maps, notes],
  );
  // How far each output being made is, from the page's one event stream.
  const progress = useStore(S.progress);
  const openMap = open?.kind === "map" ? open.id : null;
  const openNotes = open?.kind === "notes" ? open.id : null;

  return (
    <>
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
        {ready && mmLoadErr !== "" && (
          <ErrRow icon="diagram-3" title="The mind maps could not be loaded" detail={mmLoadErr} onRetry={A.reloadAll} />
        )}
        {ready && ntLoadErr !== "" && (
          <ErrRow
            icon="journal-text"
            title="The study notes could not be loaded"
            detail={ntLoadErr}
            onRetry={A.reloadAll}
          />
        )}
        {!ready ? (
          [0, 1].map((i) => (
            <div key={i} className="out-row skel-row">
              <div className="skel-line" />
            </div>
          ))
        ) : loadErr !== "" && made.length === 0 ? (
          <ErrRow title="What is in this collection could not be loaded" detail={loadErr} onRetry={A.reloadAll} />
        ) : made.length === 0 && !mmMaking && !ntMaking && mmLoadErr === "" && ntLoadErr === "" ? (
          <div className="outs-empty">
            {ro
              ? "Nothing was shared with it but its sources. Ask about them in the Ask tab."
              : "Pick a format above. What you make appears here."}
          </div>
        ) : null}
        {ready &&
          made.map((m) =>
            m.kind === "session" ? (
              <SessionItem
                key={madeKey(m)}
                s={m.s}
                at={progress[m.s.sid] ?? null}
                ro={ro}
                busy={rowBusy[`session:${m.s.sid}`] ?? ""}
                err={rowErr[`session:${m.s.sid}`] ?? ""}
                A={A}
              />
            ) : m.kind === "map" ? (
              <MapItem
                key={madeKey(m)}
                x={m.m}
                on={openMap === m.m.id}
                ro={ro}
                busy={rowBusy[`map:${m.m.id}`] ?? ""}
                err={rowErr[`map:${m.m.id}`] ?? ""}
                A={A}
                onOpen={onOpen}
              />
            ) : (
              <NotesItem
                key={madeKey(m)}
                x={m.n}
                on={openNotes === m.n.id}
                ro={ro}
                busy={rowBusy[`notes:${m.n.id}`] ?? ""}
                err={rowErr[`notes:${m.n.id}`] ?? ""}
                A={A}
                onOpen={onOpen}
              />
            ),
          )}
      </div>
    </>
  );
}

/** A deck or audio overview's row. Drawn again only when its own facts
 * change, not when another row's do. */
const SessionItem = memo(function SessionItem({
  s,
  at,
  ro,
  busy,
  err,
  A,
}: {
  s: SessionSummary;
  at: OutputProgress | null;
  ro: boolean;
  busy: string;
  err: string;
  A: PageActions;
}) {
  return (
    <SessionRow
      at={at}
      s={s}
      onChanged={() => void A.load()}
      onRename={ro ? undefined : (t) => A.rename({ kind: "session", id: s.sid }, t)}
      onDelete={ro ? undefined : () => A.remove({ kind: "session", id: s.sid })}
      busy={busy}
      err={err}
      onDismissErr={() => A.setRowErr(`session:${s.sid}`, "")}
      extra={<VideoLine sid={s.sid} ready={s.state === "ready"} ro={ro} />}
    />
  );
});

const MapItem = memo(function MapItem({
  x,
  on,
  ro,
  busy,
  err,
  A,
  onOpen,
}: {
  x: MindMapSummary;
  on: boolean;
  ro: boolean;
  busy: string;
  err: string;
  A: PageActions;
  onOpen: (o: Open) => void;
}) {
  return (
    <ItemRow
      icon="diagram-3"
      what="mind map"
      title={x.title}
      shown={x.display_title}
      facts={`${x.node_count} topics${x.focus === "" ? "" : ` · ${x.focus}`}`}
      whenMs={x.created_ms}
      on={on}
      onOpen={() => onOpen({ kind: "map", id: x.id })}
      onRename={ro ? undefined : (t) => A.rename({ kind: "map", id: x.id }, t)}
      onDelete={ro ? undefined : () => A.remove({ kind: "map", id: x.id })}
      busy={busy}
      err={err}
      onDismissErr={() => A.setRowErr(`map:${x.id}`, "")}
    />
  );
});

const NotesItem = memo(function NotesItem({
  x,
  on,
  ro,
  busy,
  err,
  A,
  onOpen,
}: {
  x: StudyNotesSummary;
  on: boolean;
  ro: boolean;
  busy: string;
  err: string;
  A: PageActions;
  onOpen: (o: Open) => void;
}) {
  return (
    <ItemRow
      icon="journal-text"
      what="study notes"
      title={x.title}
      shown={x.display_title}
      facts={`${counts(x.ideas, x.questions, x.terms)}${x.focus === "" ? "" : ` · ${x.focus}`}`}
      whenMs={x.created_ms}
      on={on}
      onOpen={() => onOpen({ kind: "notes", id: x.id })}
      onRename={ro ? undefined : (t) => A.rename({ kind: "notes", id: x.id }, t)}
      onDelete={ro ? undefined : () => A.remove({ kind: "notes", id: x.id })}
      busy={busy}
      err={err}
      onDismissErr={() => A.setRowErr(`notes:${x.id}`, "")}
    />
  );
});
