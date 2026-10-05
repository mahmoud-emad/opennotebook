// Picking several collections on the All collections page and deleting them
// at once. A port of the old `pick.rs`; see there for why it works the way
// Google Photos, Drive and Gmail do.

import { useEffect, type MouseEvent } from "react";
import { collectionDelete } from "./api";
import { ASK, askConfirm } from "./dialogs";
import { Icon } from "./Icon";
import { store, useStore } from "./store";

export type Pick = string;

export type Picks = {
  on: boolean;
  /** Started with the Select button, so it stays on with nothing ticked. */
  sticky: boolean;
  set: Pick[];
  anchor: Pick | null;
  busy: boolean;
  note: string;
};

export const EMPTY: Picks = { on: false, sticky: false, set: [], anchor: null, busy: false, note: "" };

/** Selecting that began with a checkbox ends with the last tick. */
function settle(p: Picks): Picks {
  return p.on && p.set.length === 0 && !p.sticky && !p.busy ? EMPTY : p;
}

export const start = (p: Picks): Picks => ({ ...p, on: true, sticky: true, note: "" });

/** A tick, or with shift everything from the last tick to this one. */
export function click(p: Picks, pick: Pick, shift: boolean, order: Pick[]): Picks {
  let set = [...p.set];
  const a = shift && p.anchor !== null ? order.indexOf(p.anchor) : -1;
  const b = order.indexOf(pick);
  if (shift && a >= 0 && b >= 0) {
    for (const q of order.slice(Math.min(a, b), Math.max(a, b) + 1)) if (!set.includes(q)) set.push(q);
  } else if (set.includes(pick)) set = set.filter((q) => q !== pick);
  else set.push(pick);
  return settle({ ...p, set, anchor: pick, on: true, note: "" });
}

/** Everything on screen, or nothing when everything already is. */
export function toggleAll(p: Picks, order: Pick[]): Picks {
  if (order.length > 0 && order.every((q) => p.set.includes(q)))
    return { ...p, set: [], anchor: null, sticky: true, note: "" };
  return { ...p, set: [...order], on: true, note: "" };
}

/** Drop what is no longer on screen. */
export function keep(p: Picks, order: Pick[]): Picks {
  return settle({
    ...p,
    set: p.set.filter((q) => order.includes(q)),
    anchor: p.anchor && order.includes(p.anchor) ? p.anchor : null,
  });
}

export const PICKS = store<Picks>(EMPTY);
/** What All collections shows, in its order. */
export const ORDER = store<Pick[]>([]);

const summary = (set: Pick[]) => (set.length === 1 ? "1 collection" : `${set.length} collections`);

const CONSEQUENCES = "Their sources and everything made from them are removed. This cannot be undone.";

/** Ask, then delete what is ticked. */
function confirmDelete(onDeleted: () => void): void {
  const { set, busy } = PICKS.get();
  if (set.length === 0 || busy) return;
  askConfirm(`Delete ${summary(set)}?`, `${summary(set)}. ${CONSEQUENCES}`, "Delete", () => {
    PICKS.set((p) => ({ ...p, busy: true }));
    void (async () => {
      const failed: Pick[] = [];
      for (const cid of set) {
        try {
          await collectionDelete(cid);
        } catch {
          failed.push(cid);
        }
      }
      PICKS.set((p) =>
        failed.length === 0
          ? EMPTY
          : {
              ...p,
              busy: false,
              set: failed,
              anchor: null,
              sticky: true,
              note: failed.length === 1 ? "1 could not be deleted" : `${failed.length} could not be deleted`,
            },
      );
      onDeleted();
    })();
  });
}

/** The checkbox on a card, a sibling of the card's link. */
export function PickBox({ pick, label }: { pick: Pick; label: string }) {
  const picks = useStore(PICKS);
  const on = picks.set.includes(pick);
  return (
    <button
      className={on ? "pick on" : "pick"}
      role="checkbox"
      aria-checked={on}
      aria-label={`Select ${label}`}
      title={on ? "Deselect" : "Select"}
      onClick={(e: MouseEvent) => {
        e.stopPropagation();
        e.preventDefault();
        PICKS.set((p) => click(p, pick, e.shiftKey, ORDER.get()));
      }}
    >
      <Icon name="check-lg" />
    </button>
  );
}

/** The bar at the foot of the page while selecting. Also owns the keys. */
export function PickBar({ onDeleted }: { onDeleted: () => void }) {
  const p = useStore(PICKS);
  const order = useStore(ORDER);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (ASK.get()) return;
      const tag = document.activeElement?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      const cur = PICKS.get();
      if (e.key === "Escape" && cur.on) {
        e.preventDefault();
        PICKS.set(EMPTY);
      } else if ((e.key === "a" || e.key === "A") && (e.ctrlKey || e.metaKey) && cur.on) {
        e.preventDefault();
        PICKS.set((q) => ({ ...q, note: "", set: [...ORDER.get()] }));
      } else if ((e.key === "Delete" || e.key === "Backspace") && cur.on && cur.set.length > 0) {
        e.preventDefault();
        confirmDelete(onDeleted);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onDeleted]);

  if (!p.on) return null;
  const n = p.set.length;
  const all = order.length > 0 && order.every((q) => p.set.includes(q));
  return (
    <div className="pickbar" role="toolbar" aria-label="Selection">
      <button className="icon-btn" title="Done (Esc)" aria-label="Stop selecting" onClick={() => PICKS.set(EMPTY)}>
        <Icon name="x-lg" />
      </button>
      <span className="pick-n" aria-live="polite">
        {p.busy ? "Deleting…" : n === 0 ? "Select items" : `${n} selected`}
      </span>
      {p.note && <span className="pick-note">{p.note}</span>}
      <span className="pick-sep" aria-hidden="true" />
      <button
        className="ghost"
        disabled={p.busy || order.length === 0}
        title={all ? "Deselect all" : "Select all (Ctrl+A)"}
        onClick={() => PICKS.set((q) => toggleAll(q, ORDER.get()))}
      >
        {all ? "Deselect all" : "Select all"}
      </button>
      <button className="bad" disabled={p.busy || n === 0} title="Delete (Del)" onClick={() => confirmDelete(onDeleted)}>
        <Icon name="trash" />
        Delete
      </button>
    </div>
  );
}
