// One collection: its sources on the left, its Studio in the centre, and a
// map or a set of notes open on the right while one is.
//
// The Studio is two halves of one column. Above, four tiles say what can be
// made (`Studio.tsx`); below, everything already made from these sources
// (`OutputsList.tsx`). The chat is the Ask tab beside the Studio, about this
// collection's sources.
//
// The page's state lives in stores (`state.ts`) and its work in `actions.ts`.
// This file puts the parts together and runs what the whole page shares: the
// collection's event stream, the Create panel's options from the server, and
// the estimate of what would be made.

import { useCallback, useEffect, useMemo, useState } from "react";
import { AskTab, useChatState } from "../chat";
import { Icon } from "../Icon";
import { MindMapView, estimateMap } from "../mindmap";
import { NotesView, estimateNotes, installCiteFlip } from "../notes";
import type { Open } from "../routes";
import { SETTINGS } from "../settings";
import { ShareDialog } from "../share";
import { isBuild } from "../shell";
import { SourceDrawer, useCiteOpen } from "../source";
import { useStore, useStoreSel } from "../store";
import { pageActions, type PageActions } from "./actions";
import { CollectionHeader, TABS } from "./CollectionHeader";
import { OutputsList } from "./OutputsList";
import { SourcesPanel } from "./SourcesPanel";
import { CostDialogs, Studio } from "./Studio";
import { pageState, staged, stagedCount, type CollectionPageProps, type PageState } from "./state";

export type { CollectionPageProps } from "./state";

export function CollectionPage(props: CollectionPageProps) {
  const { cid, open, onGone } = props;
  const [S] = useState(() => pageState(props));
  const chat = useChatState(cid);
  const A = useMemo(() => pageActions(cid, S, chat), [cid, S, chat]);
  const [cited, setCited] = useCiteOpen();
  // The sources beside an open map, or folded to the strip.
  const [srcOpen, setSrcOpen] = useState(false);
  // The share dialog, from the header's Share.
  const [sharing, setSharing] = useState(false);
  const missing = useStore(S.missing);
  const tab = useStore(S.tab);
  // A copy whose author did not allow edits: read, asked and played, never
  // changed or shared.
  const ro = useStoreSel(S.summary, (s) => !!s?.read_only);
  const title = useStoreSel(S.summary, (s) => s?.title ?? "");

  // The latest props, for work that finishes later: a map made in the
  // background opens only if nothing else has been opened meanwhile.
  useEffect(() => S.props.set(props));
  // Stable, so the rows that take it are not drawn again for nothing.
  const onOpen = useCallback((o: Open | null) => S.props.get().onOpen(o), [S]);

  useEffect(() => {
    A.mount();
    return A.dispose;
  }, [A]);
  useEffect(installCiteFlip, []);
  useEffect(() => A.follow(), [A]);
  useOptions(S, A);
  useEstimate(S, A, cid, ro);

  if (missing)
    return (
      <main>
        <div className="empty">
          <div className="empty-mark">
            <Icon name="collection" className="xl" />
          </div>
          <div className="empty-t">This collection is not here</div>
          <div className="empty-d">It may have been deleted, or the link is wrong.</div>
          <button onClick={onGone}>Back to My collections</button>
        </div>
      </main>
    );

  const viewer = open !== null;
  const closeViewer = () => {
    onOpen(null);
    setSrcOpen(false);
  };

  return (
    <>
      <div className={viewer ? (srcOpen ? "create with-map show-src" : "create with-map") : "create two"}>
        {/* With a map open, the sources fold to this strip; pressing it opens
            them beside the map, which stays where it is. */}
        <div className="src-strip">
          <button
            title="Show the sources"
            aria-label="Show the sources"
            aria-expanded="false"
            onClick={() => setSrcOpen(true)}
          >
            <Icon name="link-45deg" />
            Sources
          </button>
        </div>

        {/* ── sources ── */}
        <SourcesPanel S={S} A={A} onFold={() => setSrcOpen(false)} />

        {/* ── the Studio and Ask ── */}
        <section className="panel studio">
          <CollectionHeader S={S} A={A} onShare={() => setSharing(true)} />

          {tab === "studio" ? (
            <div id={TABS.studio.panelId} className="studio-body" role="tabpanel" aria-labelledby={TABS.studio.tabId}>
              {!ro && <Studio S={S} A={A} onOpen={onOpen} />}
              <OutputsList S={S} A={A} ro={ro} open={open} onOpen={onOpen} />
            </div>
          ) : (
            <AskTab st={chat} onSend={(t) => void A.sendChat(t)} />
          )}
        </section>

        {/* ── the viewer ── */}
        {open && <Viewer S={S} A={A} cid={cid} open={open} onClose={closeViewer} />}
      </div>

      {cited && <SourceDrawer key={cited.name} cid={cid} opened={cited} onClose={() => setCited(null)} />}

      {sharing && (
        <ShareDialog
          cid={cid}
          title={title}
          onClose={(changed) => {
            setSharing(false);
            if (changed) void A.load();
          }}
        />
      )}
      <CostDialogs S={S} A={A} cid={cid} />
    </>
  );
}

/** The map or notes open beside the Studio, under the name the outputs list
 * has for it, which a rename changes while it is open. */
function Viewer({
  S,
  A,
  cid,
  open,
  onClose,
}: {
  S: PageState;
  A: PageActions;
  cid: string;
  open: Open;
  onClose: () => void;
}) {
  const notesTitle = useStoreSel(S.nt.notes, (v) => v.find((n) => n.id === open.id)?.title ?? "");
  const mapTitle = useStoreSel(S.mm.maps, (v) => v.find((m) => m.id === open.id)?.title ?? "");
  if (open.kind === "notes")
    return (
      <aside className="panel apps">
        <NotesView key={open.id} cid={cid} id={open.id} title={notesTitle} onClose={onClose} />
      </aside>
    );
  return (
    <aside className="panel apps">
      <MindMapView
        key={open.id}
        cid={cid}
        id={open.id}
        title={mapTitle}
        onClose={onClose}
        onAsk={(q) => void A.askFromMap(q)}
      />
    </aside>
  );
}

/** What the Create panel offers, read from the server when the page opens and
 * again when the settings or the number of sources change, since it words
 * its hints from both. Its starting picks are followed until the person picks
 * their own here (`applyDefaults`). */
function useOptions(S: PageState, A: PageActions): void {
  const doc = useStoreSel(SETTINGS, (s) => s.doc);
  const nSrc = useStoreSel(S.srcs, stagedCount);
  useEffect(() => {
    void A.loadOptions();
  }, [A, doc, nSrc]);
}

/** Priced again when what would be made changes, and only then: an estimate
 * for a different build is worse than none, and one asked for again for the
 * same build is a wasted call. What would be made is the kind, what was
 * picked for it here, the sources it would read (which, not how many), and
 * the settings, any change of which is priced again: the server knows which
 * of them it prices by. */
function useEstimate(S: PageState, A: PageActions, cid: string, ro: boolean): void {
  const kindNow = useStore(S.chosen);
  const reads = useStoreSel(SETTINGS, (s) => s.doc);
  const sources = useStoreSel(S.srcs, (v) => staged(v).sort().join("\n"));
  const nSrc = sources === "" ? 0 : sources.split("\n").length;
  const picked = useStoreSel(S.style, (st) => (kindNow === "session" ? st : ""));
  const format = useStoreSel(S.audioFormat, (f) => (kindNow === "audio" ? f : ""));
  const length = useStoreSel(S.audioLength, (l) => (kindNow === "audio" ? l : ""));
  useEffect(() => {
    if (ro) A.dropEstimate();
    else if (kindNow !== null && isBuild(kindNow) && nSrc > 0) void A.fetchEstimate();
    else if (kindNow === "mindmap" && nSrc > 0) void estimateMap(cid, S.mm, A.signal());
    else if (kindNow === "notes" && nSrc > 0) void estimateNotes(cid, S.nt, A.signal());
    else A.dropEstimate();
    // The values below are what the estimate is of, read again by the calls
    // above from the stores; they are here so a change in any asks again.
  }, [A, S, cid, ro, kindNow, nSrc, sources, picked, format, length, reads]);
}
