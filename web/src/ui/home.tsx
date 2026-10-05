// My collections: the collections as cards or rows, and what can be done to
// one from there (open, rename, pin, share, redraw its cover, delete). The
// card's parts (its cover, what it holds) are Discover's too. A port of the
// old `home.rs`.

import { useEffect, useRef, useState, type MouseEvent } from "react";
import {
  collectionCoverRefresh,
  collectionDelete,
  collectionPin,
  collectionRetitle,
  coverUrl,
  errText,
  type CollectionSummary,
} from "./api";
import { shareCoverUrl } from "./api-share";
import { CardMenu } from "./CardMenu";
import { askConfirm, askPrompt } from "./dialogs";
import { Icon } from "./Icon";
import { ORDER, PICKS, PickBox, click } from "./pick";
import { go, routeUrl } from "./routes";
import { THEME } from "./settings";
import { ShareDialog, reusedLine } from "./share";
import { report, outputIcon, when, type Output } from "./shell";
import { useStore } from "./store";

export type Sort = "recent" | "title";
export const sortLabel = (s: Sort) => (s === "recent" ? "Most recent" : "Title");

/** Collections as a page lists them: pinned first, then the rest in the
 * chosen order. */
export function ordered(list: CollectionSummary[], sort: Sort): CollectionSummary[] {
  const v = [...list];
  if (sort === "recent") v.sort((a, b) => b.updated_ms - a.updated_ms);
  else
    v.sort((a, b) => {
      // Untitled ones last, not first.
      const ea = a.title.trim() === "";
      const eb = b.title.trim() === "";
      if (ea !== eb) return ea ? 1 : -1;
      const ta = a.title.toLowerCase();
      const tb = b.title.toLowerCase();
      return ta < tb ? -1 : ta > tb ? 1 : 0;
    });
  return [...v.filter((c) => c.pinned), ...v.filter((c) => !c.pinned)];
}

/** What a collection is called, with the stand-in for one not named yet. */
export function collTitle(title: string): string {
  return title.trim() === "" ? "Untitled collection" : title;
}

/** "1 source", "5 sources". */
export function nOf(n: number, one: string, many: string): string {
  return n === 1 ? `1 ${one}` : `${n} ${many}`;
}

/** Cards shaped like what is coming, while the list is asked for. */
export function SkelGrid({ n }: { n: number }) {
  return (
    <div className="grid" aria-busy="true">
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className="skel">
          <div className="skel-cover" />
          <div className="skel-line" />
          <div className="skel-line short" />
        </div>
      ))}
    </div>
  );
}

/** A list that could not be loaded: what failed, and another go. */
export function ListError({
  what = "Your collections could not be loaded",
  err,
  onRetry,
}: {
  /** What could not be loaded, as the heading says it. */
  what?: string;
  err: string;
  onRetry: () => void;
}) {
  return (
    <div className="empty" role="alert">
      <div className="empty-mark bad">
        <Icon name="exclamation-triangle-fill" className="xl" />
      </div>
      <div className="empty-t">{what}</div>
      <div className="empty-d">{err.replace(/\.$/, "")}. Check that the studio is running, then try again.</div>
      <button onClick={onRetry}>
        <Icon name="arrow-clockwise" />
        Try again
      </button>
    </div>
  );
}

/** The width of the canvas a cover is drawn on; it is 16:9, so 900 high. */
const COVER_W = 1600;

/** Whether the studio is still designing a collection's cover, so a list
 * should look again on its next poll. */
export function coverPending(c: CollectionSummary, coversOn: boolean): boolean {
  return coversOn && c.sources > 0 && c.cover_version.startsWith("f-") && Date.now() - c.updated_ms < 3 * 60_000;
}

/** A collection's cover: its generated page, scaled to fit the box it sits
 * in. A picture, not a control. */
export function Cover({
  cid,
  version,
  size,
  busy = false,
  share,
}: {
  cid: string;
  version: string;
  /** The share it is seen through, for a collection that is not one's own:
   * its cover is read through the share. */
  share?: string;
  /** Where the cover sits, for its size: `card`, `row`, `head` (a
   * collection's page) or `share` (a shared collection's page). */
  size: "card" | "row" | "head" | "share";
  busy?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState(
    (size === "row" ? 72 : size === "head" ? 112 : size === "share" ? 360 : 300) / COVER_W,
  );
  const [painted, setPainted] = useState("");
  const theme = useStore(THEME);
  const src = share !== undefined ? shareCoverUrl(share, version, theme) : coverUrl(cid, version, theme);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => {
      const w = e?.borderBoxSize?.[0]?.inlineSize ?? el.offsetWidth;
      setFit(Math.max(w / COVER_W, 0.01));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return (
    <div
      ref={box}
      className={`cover cover-${size}${painted === src ? " painted" : ""}`}
      style={{ ["--fit" as string]: fit }}
    >
      <iframe
        src={src}
        scrolling="no"
        loading="lazy"
        sandbox=""
        tabIndex={-1}
        aria-hidden="true"
        onLoad={() => setPainted(src)}
      />
      {busy && (
        <span className="cover-busy">
          <span className="mini-spin" />
        </span>
      )}
    </div>
  );
}

/** What a collection holds, one badge per kind it has: the kind's icon and a
 * count, then whatever is still preparing or failed, which is worth noticing
 * in a way ready work is not, and whether it is shared.
 *
 * Counts rather than a collection, so a shared collection in Discover shows
 * what its share includes with the same badges, its sources among them. */
export function OutputBadges({
  sources = 0,
  decks,
  audios,
  maps,
  notes,
  preparing = 0,
  failed = 0,
  shared = false,
  reuses = 0,
}: {
  /** Sources, shown only where they are what is on offer (a share). */
  sources?: number;
  decks: number;
  audios: number;
  maps: number;
  notes: number;
  preparing?: number;
  failed?: number;
  shared?: boolean;
  /** How often its share was reused, said beside Shared. */
  reuses?: number;
}) {
  const kinds: [Output, number, string, string][] = [
    ["session", decks, "narrated slides", "narrated slides"],
    ["audio", audios, "audio overview", "audio overviews"],
    ["mindmap", maps, "mind map", "mind maps"],
    ["notes", notes, "set of study notes", "sets of study notes"],
  ];
  const srcLabel = nOf(sources, "source", "sources");
  return (
    <span className="outs">
      {shared && (
        <span className="badge shared" title="Everyone on this studio can see and reuse it">
          <Icon name="share" />
          Shared
        </span>
      )}
      {shared && <span className="out-b">{reusedLine(reuses)}</span>}
      {sources > 0 && (
        <span className="out-b" title={srcLabel} aria-label={srcLabel}>
          <Icon name="link-45deg" />
          {sources}
        </span>
      )}
      {kinds.map(([k, n, one, many]) =>
        n > 0 ? (
          <span key={k} className="out-b" title={nOf(n, one, many)} aria-label={nOf(n, one, many)}>
            <Icon name={outputIcon[k]} />
            {n}
          </span>
        ) : null,
      )}
      {preparing > 0 && <span className="badge preparing">Preparing</span>}
      {failed > 0 && <span className="badge failed">Failed</span>}
    </span>
  );
}

/** What a card's ⋯ menu can do, so a line is matched by what it does and not
 * by where it sits in a list that changes with the collection. */
export type CardAction = "rename" | "pin" | "share" | "original" | "cover" | "delete";

/** A card's ⋯ menu: what it does, its words, and whether it is the
 * destructive one. A read-only copy offers only what the server lets it do:
 * pin, open the original and delete. */
export function cardActions(
  c: Pick<CollectionSummary, "pinned" | "shared" | "read_only">,
  hasOrigin: boolean,
  busy: boolean,
): [CardAction, string, boolean][] {
  const acts: [CardAction, string, boolean][] = [];
  if (!c.read_only) acts.push(["rename", "Rename", false]);
  acts.push(["pin", c.pinned ? "Unpin" : "Pin to top", false]);
  if (!c.read_only) acts.push(["share", c.shared ? "Edit share…" : "Share…", false]);
  if (hasOrigin) acts.push(["original", "Open the original", false]);
  if (!c.read_only) acts.push(["cover", busy ? "Designing cover…" : "Regenerate cover", false]);
  acts.push(["delete", "Delete", true]);
  return acts;
}

/** One collection on My collections: its cover, its name, how many sources
 * and when it last changed, and what it holds, with Shared when it is, and
 * where it was reused from when it was. The whole card opens it; its ⋯ menu
 * renames, pins, shares, redraws the cover and deletes it. */
export function CollectionCard({
  c,
  list,
  pickable = false,
  origin,
  onOpen,
  onChanged,
}: {
  c: CollectionSummary;
  list: boolean;
  /** Whether it can be picked to delete with others (My collections). */
  pickable?: boolean;
  /** The title of the share it was reused from, while that share exists. */
  origin?: string;
  onOpen: (cid: string) => void;
  onChanged: () => void;
}) {
  // While the ⋯ menu's Regenerate cover is at work.
  const [redrawing, setRedrawing] = useState(false);
  // The share dialog, from the ⋯ menu's Share….
  const [sharing, setSharing] = useState(false);
  const picks = useStore(PICKS);
  const busy = redrawing;
  const selecting = pickable && picks.on;
  const picked = pickable && picks.set.includes(c.cid);
  const title = collTitle(c.title);
  const untitled = c.title.trim() === "";
  const href = routeUrl({ kind: "collection", cid: c.cid, open: null });
  const onClick = (e: MouseEvent) => {
    // A modified click is the browser's: a new tab or window.
    if (!selecting && (e.ctrlKey || e.metaKey || e.shiftKey)) return;
    e.preventDefault();
    if (selecting) PICKS.set((p) => click(p, c.cid, e.shiftKey, ORDER.get()));
    else onOpen(c.cid);
  };
  const sub = `${nOf(c.sources, "source", "sources")} · ${when(c.updated_ms)}`.replace(/ · $/, "");
  // Where it came from. A link would be a link inside the card's link, so the
  // way back to the original is the ⋯ menu's Open the original.
  const reused =
    c.reused_from === "" ? null : origin !== undefined ? `Reused from ${origin}` : "Reused from a shared collection";

  const acts = cardActions(c, origin !== undefined, busy);
  const coverAt = acts.findIndex((a) => a[0] === "cover");

  const menu = (
    <CardMenu
      label={title}
      items={acts.map((a) => [a[1], a[2]])}
      off={busy && coverAt >= 0 ? [coverAt] : []}
      onPick={(i) => {
        switch (acts[i]?.[0]) {
          case "rename":
            askPrompt("Rename collection", c.title, "Save", (next) => {
              next = next.trim();
              if (next === c.title) return;
              collectionRetitle(c.cid, next)
                .catch((e) => report(`The collection could not be renamed. ${errText(e)}`))
                .finally(onChanged);
            });
            break;
          case "pin":
            collectionPin(c.cid, !c.pinned)
              .catch((e) => report(`The collection could not be ${c.pinned ? "unpinned" : "pinned"}. ${errText(e)}`))
              .finally(onChanged);
            break;
          case "share":
            setSharing(true);
            break;
          case "original":
            go({ kind: "shared", id: c.reused_from });
            break;
          case "cover":
            if (redrawing) return;
            setRedrawing(true);
            collectionCoverRefresh(c.cid)
              .catch((e) => report(`A new cover could not be designed. ${errText(e)}`))
              .finally(() => {
                setRedrawing(false);
                onChanged();
              });
            break;
          case "delete":
            askConfirm(
              `Delete “${title}” and everything made from it?`,
              "Its sources, slides, audio, mind maps and notes are removed, and so is its share if it has one. Copies others reused stay theirs. This cannot be undone.",
              "Delete",
              () => {
                collectionDelete(c.cid)
                  .catch((e) => report(`The collection could not be deleted. ${errText(e)}`))
                  .finally(onChanged);
              },
            );
            break;
        }
      }}
    />
  );

  const cover = <Cover cid={c.cid} version={c.cover_version} size={list ? "row" : "card"} busy={busy} />;
  const name = (
    <div className={untitled ? "t untitled" : "t"}>
      {c.pinned && (
        <span className="pin-i" title="Pinned">
          <Icon name="pin-angle-fill" />
        </span>
      )}
      {title}
    </div>
  );
  const pickBox = pickable ? <PickBox pick={c.cid} label={title} /> : null;
  const badges = (
    <OutputBadges
      decks={c.decks}
      audios={c.audios}
      maps={c.maps}
      notes={c.notes}
      preparing={c.preparing}
      failed={c.failed}
      shared={c.shared}
      reuses={c.reuses}
    />
  );
  const dialog = sharing && (
    <ShareDialog
      cid={c.cid}
      title={c.title}
      onClose={(changed) => {
        setSharing(false);
        if (changed) onChanged();
      }}
    />
  );

  if (list)
    return (
      <>
        <div className={picked ? "row-wrap picked" : "row-wrap"}>
          {pickBox}
          <a className="row" href={href} onClick={onClick} aria-busy={busy}>
            {cover}
            <div className="row-main">
              {name}
              <div className="row-d">
                {sub}
                {reused !== null && ` · ${reused}`}
              </div>
            </div>
            <div className="row-facts">{badges}</div>
          </a>
          {menu}
        </div>
        {dialog}
      </>
    );

  return (
    <>
      <div className={picked ? "card-wrap picked" : "card-wrap"}>
        {pickBox}
        <a className="card" href={href} onClick={onClick} aria-busy={busy}>
          {cover}
          <div className="meta">
            {name}
            <div className="sub">
              <span>{sub}</span>
            </div>
            {reused !== null && (
              <div className="sub reused">
                <Icon name="copy" />
                <span>{reused}</span>
              </div>
            )}
            {badges}
          </div>
        </a>
        {menu}
      </div>
      {dialog}
    </>
  );
}

/** The first card on My collections: the same shape as a collection's, on a
 * dashed edge because there is nothing in it yet. Starts a collection at
 * once, no form. */
export function NewCard({ busy, onNew }: { busy: boolean; onNew: () => void }) {
  return (
    <button className="card new-card" disabled={busy} onClick={onNew}>
      <span className="newc-cover">
        <span className="dc-mark">
          <Icon name="plus-lg" />
        </span>
        <span className="newc-l">{busy ? "Starting…" : "New collection"}</span>
      </span>
    </button>
  );
}
