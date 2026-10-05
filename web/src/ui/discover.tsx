// Discover, the studio's root: every collection shared on this studio, and one
// of them as a visitor sees it, with the way to reuse it. A port of the old
// app's `discover.rs`.

import { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import { errText, sleep } from "./api";
import {
  collectionReuse,
  shareFeed,
  shareItems,
  sharedPlayerUrl,
  shareView,
  type FeedKind,
  type ShareCard,
  type SharedItem,
  type SharedOutput,
  type ShareView,
} from "./api-share";
import { Cover, ListError, OutputBadges, SkelGrid, collTitle } from "./home";
import { Icon } from "./Icon";
import { follow, go, routeUrl, type View } from "./routes";
import {
  FEED_KINDS,
  FEED_SORTS,
  ShareDialog,
  appendItems,
  copyModeFact,
  feedKindLabel,
  feedSortLabel,
  itemAction,
  outputKind,
  reusedLine,
  reuseHint,
  type FeedSort,
} from "./share";
import { isBuild, mmss, notify, outputIcon, outputLabel, when, type Output } from "./shell";
import { SrcRow, fileKind, shortHost, type Src } from "./sources";

// A shared map or notes opens in the collection page's viewers, which load
// with it the first time one is opened, so Discover stays small.
const MindMapView = lazy(() => import("./mindmap").then((m) => ({ default: m.MindMapView })));
const NotesView = lazy(() => import("./notes").then((m) => ({ default: m.NotesView })));

const MINE: View = { kind: "mine" };
const DISCOVER: View = { kind: "discover" };

/** The feed: what people on this studio shared, newest or most reused first,
 * narrowed by a search. "All" lists the shared collections; a kind lists
 * what is inside them, each to play or read straight from here. */
export function DiscoverPage() {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<FeedSort>("newest");
  const [kind, setKind] = useState<FeedKind>("all");
  // The last answer, kept while the next is asked for, so a search or a change
  // of order does not blank the page between keystrokes.
  const [feed, setFeed] = useState<ShareCard[] | null>(null);
  const [items, setItems] = useState<Listed | null>(null);
  const [err, setErr] = useState("");
  const [again, setAgain] = useState(0);
  const [more, setMore] = useState<"" | "loading" | string>("");
  const [viewing, setViewing] = useState<SharedViewing | null>(null);
  // Which ask is current: a page of items asked for under an older search,
  // order or kind is dropped when it lands.
  const asked = useRef(0);

  useEffect(() => {
    let live = true;
    const n = ++asked.current;
    void (async () => {
      // A pause, so typing asks once rather than per keystroke; a newer
      // keystroke drops this one.
      if (query.trim() !== "") await sleep(250);
      if (!live) return;
      try {
        if (kind === "all") {
          const v = await shareFeed(query, sort);
          if (!live) return;
          setFeed(v);
        } else {
          const page = await shareItems(kind, query, sort);
          if (!live || n !== asked.current) return;
          setItems({ kind, list: page.items, next: page.next });
          setMore("");
        }
        setErr("");
      } catch (e) {
        if (live) setErr(errText(e));
      }
    })();
    return () => {
      live = false;
    };
  }, [query, sort, kind, again]);

  const loadMore = async () => {
    if (kind === "all" || !items || items.kind !== kind || items.next === null || more === "loading") return;
    const n = asked.current;
    setMore("loading");
    try {
      const page = await shareItems(kind, query, sort, items.next);
      if (n !== asked.current) return;
      setItems({ kind, list: appendItems(items.list, page.items), next: page.next });
      setMore("");
    } catch (e) {
      if (n === asked.current) setMore(errText(e));
    }
  };

  const q = query.trim();
  const mine = routeUrl(MINE);
  const retry = () => setAgain((n) => n + 1);
  const closeViewer = useCallback(() => setViewing(null), []);
  // Items of another kind than the one asked for are not shown while it loads.
  const shown = kind === "all" ? null : items !== null && items.kind === kind ? items : null;
  const list: unknown[] | null = kind === "all" ? feed : (shown?.list ?? null);
  const plural = feedKindLabel[kind].toLowerCase();
  return (
    <main>
      <div className="page-h">
        <div>
          <h2 className="page-t">Discover</h2>
          <p className="page-d">
            Collections people on this studio shared, and everything in them to play or read. Reuse one to make it
            yours.
          </p>
        </div>
      </div>
      <div className="feed-bar" role="search">
        <div className="search">
          <Icon name="search" />
          <input
            type="search"
            aria-label={kind === "all" ? "Search shared collections" : `Search shared ${plural}`}
            placeholder="Search by title, note or topic"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="feed-segs">
          <div className="seg" role="radiogroup" aria-label="Show">
            {FEED_KINDS.map((k) => (
              <button
                key={k}
                className={kind === k ? "on" : ""}
                role="radio"
                aria-checked={kind === k}
                aria-label={feedKindLabel[k]}
                title={feedKindLabel[k]}
                onClick={() => setKind(k)}
              >
                {k === "all" ? (
                  feedKindLabel[k]
                ) : (
                  <>
                    <Icon name={outputIcon[k]} />
                    <span className="hide-sm">{feedKindLabel[k]}</span>
                  </>
                )}
              </button>
            ))}
          </div>
          <div className="seg" role="radiogroup" aria-label="Order">
            {FEED_SORTS.map((s) => (
              <button
                key={feedSortLabel(s)}
                className={sort === s ? "on" : ""}
                role="radio"
                aria-checked={sort === s}
                onClick={() => setSort(s)}
              >
                {feedSortLabel(s)}
              </button>
            ))}
          </div>
        </div>
      </div>
      {list === null ? (
        err === "" ? (
          <SkelGrid n={8} />
        ) : (
          <ListError
            what={kind === "all" ? "Shared collections could not be loaded" : `Shared ${plural} could not be loaded`}
            err={err}
            onRetry={retry}
          />
        )
      ) : list.length === 0 && q === "" && kind === "all" ? (
        <div className="empty">
          <div className="empty-mark">
            <Icon name="compass" className="xl" />
          </div>
          <div className="empty-t">Nothing shared yet</div>
          <div className="empty-d">
            Share a collection from My collections, and it shows here for everyone on this studio to reuse.
          </div>
          <a className="btn-link" href={mine} onClick={(e) => follow(e, MINE)}>
            <Icon name="collection" />
            Go to My collections
          </a>
        </div>
      ) : list.length === 0 && q === "" && kind !== "all" ? (
        <div className="empty">
          <div className="empty-mark">
            <Icon name={outputIcon[kind]} className="xl" />
          </div>
          <div className="empty-t">No {plural} shared yet</div>
          <div className="empty-d">
            When someone shares a collection with {plural} in it, they show here to {itemAction(kind).toLowerCase()}{" "}
            straight away. Look at all shared collections meanwhile.
          </div>
          <button onClick={() => setKind("all")}>
            <Icon name="compass" />
            Show all collections
          </button>
        </div>
      ) : list.length === 0 ? (
        <div className="empty">
          <div className="empty-mark">
            <Icon name="search" className="xl" />
          </div>
          <div className="empty-t">Nothing matches “{q}”</div>
          <div className="empty-d">
            {kind === "all"
              ? "Search looks at titles, notes and topics. Try fewer words."
              : `Search looks at the titles of ${plural} and of their collections, notes and topics. Try fewer words, or another kind.`}
          </div>
          <button onClick={() => setQuery("")}>
            <Icon name="x-lg" />
            Clear search
          </button>
        </div>
      ) : (
        <>
          {err !== "" && (
            <div className="feed-err" role="alert">
              <Icon name="exclamation-triangle-fill" />
              <span className="grow">The feed could not be refreshed: {err}</span>
              <button className="link-btn" onClick={retry}>
                Try again
              </button>
            </div>
          )}
          {kind === "all" ? (
            <>
              <p className="sr-only" role="status">
                {list.length === 1 ? "1 shared collection" : `${list.length} shared collections`}
              </p>
              <div className="grid">
                {(feed ?? []).map((s) => (
                  <ShareTile key={s.share_id} s={s} />
                ))}
              </div>
            </>
          ) : (
            <>
              <p className="sr-only" role="status">
                {shown && shown.next !== null ? `${list.length} shown, more to load` : `${list.length} shown`}
              </p>
              <div className="grid">
                {(shown?.list ?? []).map((it) => (
                  <ItemTile
                    key={`${it.share_id}/${it.key}`}
                    it={it}
                    on={viewing !== null && viewing.id === it.id && viewing.shareId === it.share_id}
                    onOpen={setViewing}
                  />
                ))}
              </div>
              {more !== "" && more !== "loading" && (
                <div className="feed-err feed-more-err" role="alert">
                  <Icon name="exclamation-triangle-fill" />
                  <span className="grow">More could not be loaded: {more}</span>
                  <button className="link-btn" onClick={() => void loadMore()}>
                    Try again
                  </button>
                </div>
              )}
              {shown && shown.next !== null && (
                <div className="feed-more">
                  <button disabled={more === "loading"} aria-busy={more === "loading"} onClick={() => void loadMore()}>
                    {more === "loading" ? <span className="mini-spin" /> : <Icon name="arrow-down" />}
                    {more === "loading" ? "Loading…" : "Show more"}
                  </button>
                </div>
              )}
            </>
          )}
        </>
      )}
      {viewing && <SharedViewer v={viewing} onClose={closeViewer} />}
    </main>
  );
}

/** The items of one kind Discover holds, with where the next page starts. */
type Listed = { kind: FeedKind; list: SharedItem[]; next: number | null };

/** What a shared output's card says it is: "Untitled mind map" when it has no
 * name. */
export function itemTitle(it: Pick<SharedItem, "kind" | "title">): string {
  return it.title.trim() === "" ? `Untitled ${WHAT[outputKind(it.kind)]}` : it.title;
}

/** Where a shared output's card comes from: its collection, and who shared it. */
export function itemFrom(it: Pick<SharedItem, "collection_title" | "shared_by" | "mine">): {
  collection: string;
  by: string;
} {
  return {
    collection: collTitle(it.collection_title),
    by: it.mine ? "Shared by you" : it.shared_by.trim() === "" ? "" : `Shared by ${it.shared_by.trim()}`,
  };
}

/** One output a share includes, as a card of its own: its collection's cover,
 * what it is, and where it came from. The card plays a deck or an audio
 * overview in the player, and opens a map or notes over the page; its
 * collection's name goes to the shared collection. */
export function ItemTile({
  it,
  on = false,
  onOpen,
}: {
  it: SharedItem;
  /** Whether it is the one open in the viewer. */
  on?: boolean;
  onOpen: (v: SharedViewing) => void;
}) {
  const k = outputKind(it.kind);
  const title = itemTitle(it);
  const act = itemAction(k);
  const from = itemFrom(it);
  const made = when(it.created_ms);
  const collection: View = { kind: "shared", id: it.share_id };
  return (
    <div className="card-wrap">
      <div className={on ? "card item-card on" : "card item-card"}>
        <div className="item-cover">
          <Cover cid={it.cid} share={it.share_id} version={it.cover_version} size="card" />
          <span className="item-act" aria-hidden="true">
            <Icon name={isBuild(k) ? "play-fill" : outputIcon[k]} />
            {act}
          </span>
        </div>
        <div className="meta">
          <div className="t">
            {isBuild(k) ? (
              <a className="item-open" href={sharedPlayerUrl(it.share_id, it.id)} aria-label={`${act}: ${title}`}>
                {title}
              </a>
            ) : (
              <button
                className="item-open"
                aria-label={`${act}: ${title}`}
                aria-haspopup="dialog"
                onClick={() => onOpen({ kind: k, id: it.id, title: it.title, shareId: it.share_id, cid: it.cid })}
              >
                {title}
              </button>
            )}
          </div>
          <div className="sub">
            <Icon name={outputIcon[k]} />
            <span>{sharedFacts(it)}</span>
            {made !== "" && (
              <>
                <span aria-hidden="true">·</span>
                <span>{made}</span>
              </>
            )}
          </div>
          <div className="sub item-from">
            <a
              className="item-coll"
              href={routeUrl(collection)}
              title={`Open the shared collection “${from.collection}”`}
              onClick={(e) => follow(e, collection)}
            >
              {from.collection}
            </a>
            {from.by !== "" && (
              <>
                <span aria-hidden="true">·</span>
                <span>{from.by}</span>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/** One shared collection in the feed: the same card as a collection of one's
 * own, with its note, what it includes, and how often it was reused. */
function ShareTile({ s }: { s: ShareCard }) {
  const view: View = { kind: "shared", id: s.share_id };
  const title = collTitle(s.title);
  const untitled = s.title.trim() === "";
  const shared = when(s.updated_ms);
  return (
    <div className="card-wrap">
      <a className="card" href={routeUrl(view)} onClick={(e) => follow(e, view)}>
        <Cover cid={s.cid} share={s.share_id} version={s.cover_version} size="card" />
        <div className="meta">
          <div className={untitled ? "t untitled" : "t"}>{title}</div>
          {s.note.trim() !== "" && <p className="card-note">{s.note}</p>}
          <div className="sub">
            <span>{reusedLine(s.reuses)}</span>
            {shared !== "" && (
              <>
                <span aria-hidden="true">·</span>
                <span>Shared {shared}</span>
              </>
            )}
            <span aria-hidden="true">·</span>
            <span>{copyModeFact(s.allow_edits)}</span>
          </div>
          <OutputBadges sources={s.sources} decks={s.decks} audios={s.audios} maps={s.maps} notes={s.notes} />
        </div>
      </a>
    </div>
  );
}

/** What a shared output says under its name. */
export function sharedFacts(o: Pick<SharedOutput, "kind" | "slide_count" | "duration_ms">): string {
  const k = outputKind(o.kind);
  if (k === "session") {
    let f = `${o.slide_count} slides`;
    if (o.duration_ms > 0) f += ` · ${mmss(o.duration_ms)}`;
    return f;
  }
  if (k === "audio" && o.duration_ms > 0) return `Audio overview · ${mmss(o.duration_ms)}`;
  return outputLabel[k];
}

/** A shared source as the read-only row the sources panel draws. */
export function sharedSrc(name: string, title: string, url: string, chars: number): Src {
  const words = Math.floor(chars / 6);
  return {
    name: title === "" ? name : title,
    detail: url === "" ? `${fileKind(name)} · ${words} words` : `${shortHost(url)} · ${words} words`,
    ok: true,
    url,
    icon: "",
    // Empty: not one of yours, so the row offers no remove.
    file: "",
  };
}

/** A map or notes of a share, open over the page in the viewer the collection
 * page uses, read only. */
export type SharedViewing = { kind: Output; id: string; title: string; shareId: string; cid: string };

/** A shared map or notes, over the page, read only, read through its share. */
function SharedViewer({ v, onClose }: { v: SharedViewing; onClose: () => void }) {
  // Escape closes it wherever focus is: it opens from a card or a row that
  // keeps focus behind the dialog.
  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  return (
    <>
      <div className="set-veil" onClick={onClose} />
      <div
        className="set-dialog viewer-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={`${outputLabel[v.kind]}: ${v.title}`}
      >
        <Suspense fallback={null}>
          {v.kind === "mindmap" ? (
            <MindMapView
              key={v.id}
              cid={v.cid}
              shareId={v.shareId}
              id={v.id}
              title={v.title}
              readOnly
              onClose={onClose}
            />
          ) : (
            <NotesView
              key={v.id}
              cid={v.cid}
              shareId={v.shareId}
              id={v.id}
              title={v.title}
              readOnly
              onClose={onClose}
            />
          )}
        </Suspense>
      </div>
    </>
  );
}

/** One output on a shared collection's page: the outputs list's row, with
 * what opening it does said on it, Play, Listen or Open. */
export function SharedRow({
  o,
  shareId,
  on,
  onOpen,
}: {
  o: SharedOutput;
  shareId: string;
  on: boolean;
  onOpen: () => void;
}) {
  const k = outputKind(o.kind);
  const shown = o.title.trim() === "" ? `Untitled ${WHAT[k]}` : o.title;
  const act = itemAction(k);
  const w = when(o.created_ms);
  const body = (
    <>
      <span className="out-i">
        <Icon name={outputIcon[k]} />
      </span>
      <span className="out-main">
        <span className="out-t">{shown}</span>
        <span className="out-d num">
          {sharedFacts(o)}
          {w !== "" && ` · ${w}`}
        </span>
      </span>
      <span className="out-act">
        <Icon name={isBuild(k) ? "play-fill" : outputIcon[k]} />
        {act}
      </span>
    </>
  );
  return (
    <div className={on ? "out-row on" : "out-row"}>
      {isBuild(k) ? (
        <a className="out-open" href={sharedPlayerUrl(shareId, o.sid)} aria-label={`${act}: ${shown}`}>
          {body}
        </a>
      ) : (
        <button className="out-open" aria-label={`${act}: ${shown}`} aria-haspopup="dialog" onClick={onOpen}>
          {body}
        </button>
      )}
    </div>
  );
}

/** What a shared output is called in a sentence. */
const WHAT: Record<Output, string> = {
  audio: "audio overview",
  mindmap: "mind map",
  notes: "study notes",
  session: "narrated slides",
};

/** One shared collection as a visitor sees it: what it is, what it includes,
 * and Reuse collection, which copies it into a new collection of one's own. */
export function SharedPage({
  shareId,
  setCrumb,
}: {
  shareId: string;
  /** The shared collection's name, for the top bar's breadcrumb. */
  setCrumb: (t: string) => void;
}) {
  const [got, setGot] = useState<ShareView | null>(null);
  const [err, setErr] = useState("");
  const [reusing, setReusing] = useState(false);
  const [reuseErr, setReuseErr] = useState("");
  const [viewing, setViewing] = useState<SharedViewing | null>(null);
  const [editing, setEditing] = useState(false);
  const closeViewer = useCallback(() => setViewing(null), []);

  const load = useCallback(async () => {
    setErr("");
    try {
      const v = await shareView(shareId);
      const card = v.found ? v.card : undefined;
      setCrumb(card ? collTitle(card.title) : "");
      setGot(v);
    } catch (e) {
      setErr(errText(e));
    }
  }, [shareId, setCrumb]);
  useEffect(() => {
    setCrumb("");
    // The page is set when the server answers.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load, setCrumb]);

  const reuse = async () => {
    if (reusing) return;
    setReusing(true);
    setReuseErr("");
    try {
      const cid = await collectionReuse(shareId);
      notify(
        got?.card?.allow_edits === false ? "Reused as a read-only copy of your own." : "Reused. It’s yours now.",
      );
      go({ kind: "collection", cid, open: null });
    } catch (e) {
      setReuseErr(`It could not be reused: ${errText(e)}`);
    }
    setReusing(false);
  };

  if (!got) {
    if (err !== "")
      return (
        <main>
          <ListError what="This shared collection could not be loaded" err={err} onRetry={() => void load()} />
        </main>
      );
    return (
      <main aria-busy="true">
        <div className="share-top">
          <div className="skel share-cover-skel">
            <div className="skel-cover" />
          </div>
          <div className="share-info">
            <div className="skel-line" />
            <div className="skel-line" />
            <div className="skel-line short" />
          </div>
        </div>
      </main>
    );
  }
  const card = got.found ? got.card : undefined;
  if (!card)
    return (
      <main>
        <div className="empty">
          <div className="empty-mark">
            <Icon name="share" className="xl" />
          </div>
          <div className="empty-t">This collection is not shared any more</div>
          <div className="empty-d">
            Its owner stopped sharing it, or the link is wrong. Copies people already made are still theirs.
          </div>
          <a className="btn-link" href={routeUrl(DISCOVER)} onClick={(e) => follow(e, DISCOVER)}>
            <Icon name="compass" />
            Back to Discover
          </a>
        </div>
      </main>
    );
  const title = collTitle(card.title);
  const shared = when(card.updated_ms);
  const srcs = got.sources.map((s) => sharedSrc(s.name, s.title, s.url, s.chars));
  return (
    <>
      <main className="share-page">
        <section className="share-top" aria-labelledby="share-page-t">
          <Cover cid={card.cid} share={shareId} version={card.cover_version} size="share" />
          <div className="share-info">
            <h2 id="share-page-t" className={card.title.trim() === "" ? "page-t untitled" : "page-t"}>
              {title}
            </h2>
            {card.note.trim() !== "" && <p className="share-note-p">{card.note}</p>}
            <div className="share-facts num">
              <OutputBadges
                sources={card.sources}
                decks={card.decks}
                audios={card.audios}
                maps={card.maps}
                notes={card.notes}
              />
              <span>{reusedLine(card.reuses)}</span>
              <span>{copyModeFact(card.allow_edits)}</span>
              {shared !== "" && <span>Shared {shared}</span>}
            </div>
            <div className="share-actions">
              <button
                className="primary"
                disabled={reusing}
                aria-busy={reusing}
                title="Copy what is shared into a new collection of your own"
                onClick={() => void reuse()}
              >
                {reusing ? <span className="mini-spin" /> : <Icon name="copy" />}
                {reusing ? "Reusing…" : "Reuse collection"}
              </button>
              {/* Its owner changes it from here too. */}
              {card.mine && (
                <button
                  className="ghost"
                  title="Change what this share includes, or stop sharing it"
                  onClick={() => setEditing(true)}
                >
                  <Icon name="pencil" />
                  Edit share
                </button>
              )}
            </div>
            {reuseErr !== "" ? (
              <div className="opt-err" role="alert">
                {reuseErr}
              </div>
            ) : (
              <p className="share-hint">{reuseHint(card.allow_edits)}</p>
            )}
          </div>
        </section>

        {srcs.length > 0 && (
          <section className="share-sec" aria-labelledby="share-src-h">
            <div className="sec-h">
              <span id="share-src-h" className="sec-t">
                Sources
              </span>
              <span className="sec-n num">{srcs.length}</span>
            </div>
            <div className="srclist share-srcs">
              {srcs.map((s, n) => (
                <SrcRow key={n} s={s} />
              ))}
            </div>
          </section>
        )}

        <section className="share-sec" aria-labelledby="share-out-h">
          <div className="sec-h">
            <span id="share-out-h" className="sec-t">
              Outputs
            </span>
            {got.outputs.length > 0 && <span className="sec-n num">{got.outputs.length}</span>}
          </div>
          <div className="outs-list">
            {got.outputs.length === 0 && (
              <div className="outs-empty">
                {card.allow_edits
                  ? "Only the sources were shared. Reuse it to make something from them."
                  : "Only the sources were shared. Reuse it to read them and ask about them."}
              </div>
            )}
            {got.outputs.map((o) => {
              const k = outputKind(o.kind);
              return (
                <SharedRow
                  key={o.key}
                  o={o}
                  shareId={shareId}
                  on={viewing !== null && viewing.id === o.id && viewing.kind === k}
                  onOpen={() => setViewing({ kind: k, id: o.id, title: o.title, shareId, cid: card.cid })}
                />
              );
            })}
          </div>
        </section>
      </main>

      {viewing && <SharedViewer v={viewing} onClose={closeViewer} />}
      {editing && (
        <ShareDialog
          cid={card.cid}
          title={card.title}
          onClose={(changed) => {
            setEditing(false);
            if (changed) void load();
          }}
        />
      )}
    </>
  );
}
