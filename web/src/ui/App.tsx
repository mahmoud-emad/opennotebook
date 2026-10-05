// opennotebook, the end-user app: the shell, Discover and My collections. A
// port of the old app's `main.rs` `App`, screen for screen.

import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createCollection, errText, followCollection, listCollections, sleep, type CollectionSummary } from "./api";
import { AskDialog } from "./dialogs";
import { DiscoverPage, SharedPage } from "./discover";
import {
  CollectionCard,
  ListError,
  NewCard,
  PageSkel,
  SkelGrid,
  collTitle,
  ordered,
  sortLabel,
  type Sort,
} from "./home";
import { Icon } from "./Icon";
import { EMPTY, ORDER, PICKS, PickBar, keep, start } from "./pick";
import { follow, routeFromLocation, routeUrl, sameView, setNav, setRoute, type Open, type View } from "./routes";
import { GENERAL, SETTINGS, openSettings, reloadSettings } from "./settings";
import { FLASH, NOTICE, SNACK, SNACK_MS, snack, type Output } from "./shell";
import { useStore } from "./store";

// The collection page, the map and the notes load when a collection is first
// opened, so the first screen stays small.
const CollectionPage = lazy(() =>
  import("./collection/CollectionPage").then((m) => ({ default: m.CollectionPage })),
);
// Settings load the first time they are opened: most visits never open them.
/** How many cards of My collections follow their collection's stream at once. */
const FOLLOW_MAX = 3;
/** How often My collections is read again while a card cannot follow its stream. */
const LIST_FALLBACK_MS = 30_000;

const SettingsDialog = lazy(() => import("./SettingsDialog").then((m) => ({ default: m.SettingsDialog })));
// The player is a page of its own, loaded when an output is first played.
const PlayerPage = lazy(() => import("./player").then((m) => ({ default: m.PlayerPage })));

export function App() {
  const [view, setViewRaw] = useState<View>(routeFromLocation);
  const [collections, setCollections] = useState<CollectionSummary[]>([]);
  // Whether the first list has answered: without it the page could not tell
  // "you have none" from "we have not asked yet".
  const [loaded, setLoaded] = useState(false);
  const [loadErr, setLoadErr] = useState("");
  const [grid, setGrid] = useState(true);
  const [sort, setSort] = useState<Sort>("recent");
  const [sortOpen, setSortOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  // The kind to open a new collection on, from an address that asked for one.
  const [preselect, setPreselect] = useState<[string, Output] | null>(null);
  // The open collection's name, for the breadcrumb; the page keeps it.
  const [crumb, setCrumb] = useState("");
  const flash = useStore(FLASH);
  const notice = useStore(NOTICE);
  const snackS = useStore(SNACK);
  const settings = useStore(SETTINGS);
  const picks = useStore(PICKS);
  const viewRef = useRef(view);
  useEffect(() => {
    viewRef.current = view;
  }, [view]);

  // Every change of screen goes through here. A tile asked for by an old
  // address belongs to the collection it made; anywhere else, it is forgotten.
  const setView = useCallback((v: View) => {
    setPreselect((p) => (p && (v.kind !== "collection" || v.cid !== p[0]) ? null : p));
    // An error is about the screen it happened on; another screen starts
    // clear. A success notice keeps its own few seconds, since it is often
    // said on the way to the screen it describes.
    if (!sameView(v, viewRef.current)) FLASH.set("");
    setViewRaw(v);
  }, []);

  // Any screen moves to another through `routes.go`.
  useEffect(() => {
    setNav(setView);
    return () => setNav(null);
  }, [setView]);

  useEffect(() => {
    void reloadSettings();
  }, []);

  // The snackbar goes on its own; a newer message is never cleared by an
  // older one's timer.
  useEffect(() => {
    if (!snackS) return;
    const n = snackS[0];
    const t = setTimeout(() => SNACK.get()?.[0] === n && SNACK.set(null), SNACK_MS);
    return () => clearTimeout(t);
  }, [snackS]);

  // Which read of the list is the latest: a poll and a reload after a delete
  // can cross, and the older answer must not put back what was deleted.
  const reloadSeq = useRef(0);
  const reload = useCallback(async () => {
    const my = ++reloadSeq.current;
    try {
      const got = await listCollections();
      if (my !== reloadSeq.current) return;
      setCollections(got);
      setLoadErr("");
    } catch (e) {
      if (my !== reloadSeq.current) return;
      setLoadErr(errText(e));
    }
    // Set even when the call failed, or skeletons would spin forever.
    setLoaded(true);
  }, []);

  const list = useMemo(() => ordered(collections, sort), [collections, sort]);
  const n = list.length;

  // What My collections shows, in its order: what a shift-click's range
  // runs over and what "Select all" means.
  useEffect(() => {
    const order = list.map((c) => c.cid);
    ORDER.set(order);
    if (PICKS.get().set.some((p) => !order.includes(p))) PICKS.set((p) => keep(p, order));
  }, [list]);

  // The address bar follows the screen, and the browser's Back button works.
  useEffect(() => {
    const v = view;
    if (v.kind === "new") return;
    const here = routeFromLocation();
    if (!sameView(here, v)) {
      // Opening a map beside the same collection replaces; anything else is a
      // page of its own.
      const beside = here.kind === "collection" && v.kind === "collection" && here.cid === v.cid;
      setRoute(v, beside);
    } else if (window.location.pathname !== routeUrl(v)) {
      // The same screen under an old address: correct it in place.
      setRoute(v, true);
    }
    // Selecting is a mode of My collections; leaving it ends it.
    if (v.kind !== "mine" && PICKS.get().on) PICKS.set(EMPTY);
    // Back on the list: it may have changed under the collection page. The
    // list is set when the server answers, not during this effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (v.kind === "mine") void reload();
  }, [view, reload]);

  useEffect(() => {
    const onPop = () => setView(routeFromLocation());
    window.addEventListener("popstate", onPop);
    // Files dropped anywhere but the sources panel are refused, not opened.
    const onDrag = (e: DragEvent) => {
      if (e.dataTransfer?.types.includes("Files") && !e.defaultPrevented) {
        e.preventDefault();
        e.dataTransfer.dropEffect = "none";
      }
    };
    window.addEventListener("dragover", onDrag);
    window.addEventListener("drop", onDrag);
    return () => {
      window.removeEventListener("popstate", onPop);
      window.removeEventListener("dragover", onDrag);
      window.removeEventListener("drop", onDrag);
    };
  }, [setView]);

  // An old `new-…` address: make the collection, then open it on that kind.
  useEffect(() => {
    if (view.kind !== "new") return;
    const kind = view.output;
    createCollection().then(
      (cid) => {
        setPreselect([cid, kind]);
        const v: View = { kind: "collection", cid, open: null };
        setRoute(v, true);
        setView(v);
      },
      (e) => {
        snack(errText(e));
        setRoute({ kind: "discover" }, true);
        setView({ kind: "discover" });
      },
    );
  }, [view, setView]);

  const newCollection = () => {
    if (creating) return;
    setCreating(true);
    createCollection()
      .then(
        (cid) => {
          setPreselect(null);
          setView({ kind: "collection", cid, open: null });
        },
        // The studio's own words: the empty-collection limit is decided there.
        (e) => snack(errText(e)),
      )
      .finally(() => setCreating(false));
  };

  // While My collections is on screen, a card whose collection is still being
  // made (an output, its name, its cover) follows that collection's event
  // stream until the server says it is done; nothing is read on a timer. A
  // browser holds only a few connections to one host, so only the first few
  // follow; past those, or while a stream is down, the list is read again,
  // slowly.
  const following = useMemo(
    () =>
      view.kind === "mine"
        ? collections
            .filter((c) => c.busy)
            .map((c) => c.cid)
            .join(",")
        : "",
    [collections, view.kind],
  );
  useEffect(() => {
    if (following === "") return;
    const ids = following.split(",");
    const down = new Set<string>(ids.slice(FOLLOW_MAX));
    const closes = ids.slice(0, FOLLOW_MAX).map((cid) =>
      followCollection(cid, {
        collection: (c) => setCollections((v) => v.map((x) => (x.cid === c.cid ? c : x))),
        gone: () => setCollections((v) => v.filter((x) => x.cid !== cid)),
        up: (ok) => (ok ? down.delete(cid) : down.add(cid)),
      }),
    );
    const stop = new AbortController();
    void (async () => {
      while (!stop.signal.aborted) {
        await sleep(LIST_FALLBACK_MS, stop.signal);
        if (!stop.signal.aborted && down.size > 0) await reload();
      }
    })();
    return () => {
      stop.abort();
      closes.forEach((close) => close());
    };
  }, [following, reload]);

  const openColl = (cid: string) => {
    setPreselect(null);
    setView({ kind: "collection", cid, open: null });
  };
  // The pages whose one primary is New collection. A collection's is its own
  // Generate, and a shared one's is Reuse collection.
  const onList = view.kind === "discover" || view.kind === "mine";
  const changed = () => void reload();
  const navLink = (target: View, label: string, icon: string) => {
    const here = view.kind === target.kind;
    return (
      <a
        className={here ? "nav-l on" : "nav-l"}
        href={routeUrl(target)}
        aria-label={label}
        aria-current={here ? "page" : undefined}
        onClick={(e) => follow(e, target)}
      >
        <Icon name={icon} />
        <span className="hide-sm">{label}</span>
      </a>
    );
  };

  // The player has no top bar of the app's: it is a full page with its own.
  if (view.kind === "play")
    return (
      <Suspense fallback={<PageSkel />}>
        <PlayerPage key={`${view.sid}|${view.share ?? ""}`} sid={view.sid} share={view.share} />
      </Suspense>
    );

  return (
    <>
      <header>
        {/* The logo is the way to the root, Discover, as on every site. */}
        <button
          className="brand"
          title="Discover"
          aria-label="Studio: Discover"
          onClick={() => setView({ kind: "discover" })}
        >
          <span className="mark">
            <Icon name="collection-play" />
          </span>
          <h1 className="hide-sm">Studio</h1>
        </button>
        {/* The two places to be; the one you are on says so. */}
        <nav className="nav" aria-label="Main">
          {navLink({ kind: "discover" }, "Discover", "compass")}
          {navLink({ kind: "mine" }, "My collections", "collection")}
        </nav>
        {/* Where you are, below those two. */}
        {(view.kind === "collection" || view.kind === "shared") && (
          <span className="crumb">
            <span className="sep" aria-hidden="true">
              /
            </span>
            <span className={crumb.trim() === "" ? "here untitled" : "here"}>{collTitle(crumb)}</span>
          </span>
        )}
        <span className="grow" />
        {view.kind === "mine" && n > 0 && (
          <>
            <button
              className={picks.on ? "ghost on" : "ghost"}
              title={picks.on ? "Stop selecting (Esc)" : "Select several"}
              aria-label="Select"
              aria-pressed={picks.on}
              onClick={() => PICKS.set((p) => (p.on ? EMPTY : start(p)))}
            >
              <Icon name="check2-square" />
              <span className="hide-sm">Select</span>
            </button>
            <div className="seg">
              <button
                className={grid ? "on" : ""}
                onClick={() => setGrid(true)}
                title="Grid"
                aria-label="Show as a grid"
                aria-pressed={grid}
              >
                <Icon name="grid-3x3-gap" />
              </button>
              <button
                className={grid ? "" : "on"}
                onClick={() => setGrid(false)}
                title="List"
                aria-label="Show as a list"
                aria-pressed={!grid}
              >
                <Icon name="list-ul" />
              </button>
            </div>
            <div className="menu">
              <button
                className="ghost"
                title={`Sort: ${sortLabel(sort)}`}
                aria-label={`Sort: ${sortLabel(sort)}`}
                aria-haspopup="menu"
                aria-expanded={sortOpen}
                onClick={() => setSortOpen(!sortOpen)}
              >
                <Icon name="sort-down" />
                <span className="hide-sm">{sortLabel(sort)}</span>
              </button>
              {sortOpen && (
                <>
                  <div
                    className="dots-veil"
                    onClick={(e) => {
                      e.stopPropagation();
                      e.preventDefault();
                      setSortOpen(false);
                    }}
                  />
                  <div className="menu-pop" role="menu" aria-label="Sort by">
                    {(["recent", "title"] as Sort[]).map((opt) => (
                      <button
                        key={opt}
                        className={sort === opt ? "on" : ""}
                        role="menuitemradio"
                        aria-checked={sort === opt}
                        onClick={() => {
                          setSort(opt);
                          setSortOpen(false);
                        }}
                      >
                        {sortLabel(opt)}
                      </button>
                    ))}
                  </div>
                </>
              )}
            </div>
          </>
        )}
        {/* The one primary of a list page. Only there: on a collection it made
            another empty collection per click. */}
        {onList && (
          <button
            className="primary"
            title="Start a new collection of sources"
            aria-label="New collection"
            disabled={creating}
            onClick={newCollection}
          >
            <Icon name="plus-lg" />
            <span className="hide-sm">New collection</span>
          </button>
        )}
        <button className="icon-btn" title="Settings" aria-label="Settings" onClick={() => openSettings(GENERAL)}>
          <Icon name="gear" className="lg" />
        </button>
      </header>
      {(notice || flash) && (
        <div className="flash-stack">
      {notice && (
        <div className="flash ok" role="status">
          <Icon name="check-circle-fill" />
          <span className="grow">{notice}</span>
          <button className="icon-btn" title="Dismiss" aria-label="Dismiss" onClick={() => NOTICE.set("")}>
            <Icon name="x-lg" />
          </button>
        </div>
      )}
      {flash && (
        <div className="flash" role="alert">
          <Icon name="exclamation-triangle-fill" />
          <span className="grow">{flash}</span>
          <button className="icon-btn" title="Dismiss" aria-label="Dismiss" onClick={() => FLASH.set("")}>
            <Icon name="x-lg" />
          </button>
        </div>
      )}
        </div>
      )}
      {snackS && (
        <div key={snackS[0]} className="snack" role="alert">
          <Icon name="exclamation-triangle-fill" />
          <span className="grow">{snackS[1]}</span>
          <button className="icon-btn" title="Dismiss" aria-label="Dismiss" onClick={() => SNACK.set(null)}>
            <Icon name="x-lg" />
          </button>
        </div>
      )}
      {settings.open !== null && (
        // The veil at once, so the click is answered while the dialog loads.
        <Suspense fallback={<div className="set-veil" />}>
          <SettingsDialog onClose={() => void reloadSettings()} />
        </Suspense>
      )}
      <AskDialog />

      {view.kind === "collection" && (
        <Suspense fallback={<PageSkel />}>
        <CollectionPage
          // Keyed on the collection, so opening another mounts a fresh page.
          key={view.cid}
          cid={view.cid}
          open={view.open}
          start={preselect && preselect[0] === view.cid ? preselect[1] : null}
          setCrumb={setCrumb}
          onOpen={(o: Open | null) => setView({ kind: "collection", cid: view.cid, open: o })}
          onGone={() => setView({ kind: "mine" })}
        />
        </Suspense>
      )}
      {view.kind === "shared" && <SharedPage key={view.id} shareId={view.id} setCrumb={setCrumb} />}
      {view.kind === "new" && (
        <main>
          <div className="empty">
            <div className="spinner" />
            <div className="empty-t">Starting a collection…</div>
          </div>
        </main>
      )}
      {view.kind === "discover" && <DiscoverPage />}
      {view.kind === "mine" && (
        <>
          <PickBar onDeleted={changed} />
          <main className={picks.on ? "selecting" : ""}>
            <div className="page-h">
              <div>
                <h2 className="page-t">My collections</h2>
                {loaded && n > 0 && <p className="page-d num">{n === 1 ? "1 collection" : `${n} collections`}</p>}
              </div>
            </div>
            {!loaded ? (
              <SkelGrid n={8} />
            ) : loadErr && n === 0 ? (
              <ListError err={loadErr} onRetry={changed} />
            ) : (
              <>
                <div className={grid || n === 0 ? "grid" : "grid list"}>
                  {/* The way to start one, first, in the shape of what it
                      makes. A list has the top bar's button. */}
                  {(grid || n === 0) && <NewCard busy={creating} onNew={newCollection} />}
                  {list.map((c) => (
                    <CollectionCard
                      key={c.cid}
                      c={c}
                      list={!grid}
                      pickable
                      onOpen={openColl}
                      onChanged={changed}
                    />
                  ))}
                </div>
                {n === 0 && (
                  <p className="home-lead">
                    A collection holds your sources and everything you make from them: narrated slides, audio, mind
                    maps and notes. Or reuse one from{" "}
                    <a href={routeUrl({ kind: "discover" })} onClick={(e) => follow(e, { kind: "discover" })}>
                      Discover
                    </a>
                    .
                  </p>
                )}
              </>
            )}
          </main>
        </>
      )}
    </>
  );
}
