// The head of a collection's Studio column: its cover, its name to edit, what
// it holds, Share, and the Studio and Ask tabs.

import { useEffect, useState } from "react";
import { shareView } from "../api-share";
import { THREAD_ID } from "../chat";
import { Cover, collTitle } from "../home";
import { Icon } from "../Icon";
import { follow, routeUrl, type View } from "../routes";
import { keys, useSettings } from "../settings";
import { READ_ONLY_TAIL, readOnlyOf, reusedLine } from "../share";
import { focusId } from "../shell";
import { useStore, useStoreSel } from "../store";
import type { PageActions } from "./actions";
import { stagedCount, type PageState, type Tab } from "./state";

export const TABS: Record<Tab, { label: string; icon: string; tabId: string; panelId: string }> = {
  studio: { label: "Studio", icon: "stars", tabId: "tab-studio", panelId: "panel-studio" },
  // The Ask panel is the chat thread itself.
  ask: { label: "Ask", icon: "chat-dots", tabId: "tab-ask", panelId: THREAD_ID },
};

export function CollectionHeader({ S, A, onShare }: { S: PageState; A: PageActions; onShare: () => void }) {
  const summary = useStore(S.summary);
  const title = useStore(S.title);
  const tab = useStore(S.tab);
  const nSrc = useStoreSel(S.srcs, stagedCount);
  const nOut =
    useStoreSel(S.outputs, (v) => v.length) +
    useStoreSel(S.mm.maps, (v) => v.length) +
    useStoreSel(S.nt.notes, (v) => v.length);
  const renaming = useStore(S.renaming);
  // Until the sources and every kind of output have answered, the counts
  // would say "0 sources · 0 outputs" of a collection that has some.
  const srcsLoaded = useStore(S.srcsLoaded);
  const loaded = useStore(S.loaded);
  const mmLoaded = useStore(S.mm.loaded);
  const ntLoaded = useStore(S.nt.loaded);
  const counted = srcsLoaded && loaded && mmLoaded && ntLoaded;
  const cfg = useSettings();
  // A copy whose author did not allow edits: read, asked and played, never
  // changed or shared. The server refuses those; the page does not offer them.
  const ro = !!summary?.read_only;
  const originId = ro ? (summary?.reused_from ?? "") : "";
  // A read-only copy's original: its title while its share is there, null
  // once it is not (or could not be read), undefined until it is known.
  const [origin, setOrigin] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    if (originId === "") return;
    const ctrl = new AbortController();
    shareView(originId, ctrl.signal).then(
      (v) => !ctrl.signal.aborted && setOrigin(v.found && v.card ? collTitle(v.card.title) : null),
      () => !ctrl.signal.aborted && setOrigin(null),
    );
    return () => ctrl.abort();
  }, [originId]);
  // Settings the page says something about. Until they are read the hints
  // stay quiet rather than show a value that may not be the one in force.
  const naming = cfg.on(keys.AUTO_NAME) && !!summary?.title_auto;

  return (
    <div className="studio-head">
      {summary && <Cover cid={summary.cid} version={summary.cover_version} size="head" />}
      <div className="sh-main">
        <input
          className="make-title"
          type="text"
          aria-label="Collection name"
          title={
            ro
              ? "A read-only copy keeps its name"
              : naming
                ? "Named from its sources until you name it. Turn off in Settings › Generation defaults."
                : "Name this collection"
          }
          readOnly={ro}
          placeholder="Untitled collection"
          maxLength={120}
          value={title}
          onChange={(e) => {
            S.typing.set(true);
            S.title.set(e.target.value);
          }}
          onKeyDown={(e) => e.key === "Enter" && A.commitTitle()}
          onBlur={A.commitTitle}
        />
        <div className="sh-sub num">
          {counted ? (
            <>
              <span>{nSrc === 1 ? "1 source" : `${nSrc} sources`}</span>
              <span>·</span>
              <span>{nOut === 1 ? "1 output" : `${nOut} outputs`}</span>
            </>
          ) : (
            <span className="dim">Loading…</span>
          )}
          {naming && title.trim() === "" && nSrc > 0 ? (
            <>
              <span>·</span>
              <span className="sh-auto">Naming it from its sources…</span>
            </>
          ) : naming && title.trim() !== "" ? (
            <>
              <span>·</span>
              <span className="sh-auto">Named from its sources</span>
            </>
          ) : null}
          {renaming && (
            <>
              <span>·</span>
              <span className="sh-auto">Saving the name…</span>
            </>
          )}
          {summary?.shared && (
            <>
              <span>·</span>
              <span>{reusedLine(summary.reuses)}</span>
            </>
          )}
        </div>
        {ro && <ReadOnlyNote id={originId} origin={origin} />}
      </div>
      {/* Secondary: this page's one primary is its Generate. */}
      {summary && !ro && (
        <button
          className="share-btn"
          title={
            summary.shared
              ? "Shared with everyone on this studio. Change what it includes, or stop sharing."
              : "Let everyone on this studio see it and reuse a copy"
          }
          aria-haspopup="dialog"
          onClick={onShare}
        >
          <Icon name="share" />
          {summary.shared ? "Shared" : "Share"}
        </button>
      )}
      {/* A tablist as the ARIA pattern has it: arrow keys move between
          the tabs, only the selected one is in the Tab order, and each
          names the panel it shows. */}
      <div
        className="seg"
        role="tablist"
        aria-label="View"
        onKeyDown={(e) => {
          const next: Tab | null =
            e.key === "ArrowLeft" || e.key === "Home"
              ? "studio"
              : e.key === "ArrowRight" || e.key === "End"
                ? "ask"
                : null;
          if (next === null) return;
          e.preventDefault();
          S.tab.set(next);
          focusId(TABS[next].tabId);
        }}
      >
        {(["studio", "ask"] as Tab[]).map((t) => (
          <button
            key={TABS[t].tabId}
            id={TABS[t].tabId}
            className={tab === t ? "on" : ""}
            role="tab"
            tabIndex={tab === t ? 0 : -1}
            aria-selected={tab === t}
            aria-controls={TABS[t].panelId}
            onClick={() => S.tab.set(t)}
          >
            <Icon name={TABS[t].icon} />
            {TABS[t].label}
          </button>
        ))}
      </div>
    </div>
  );
}

/** The line under a read-only copy's header: what it is a copy of, a link to
 * that share while it is there, and why nothing here can be changed. */
function ReadOnlyNote({ id, origin }: { id: string; origin: string | null | undefined }) {
  const view: View = { kind: "shared", id };
  return (
    <p className="share-hint ro-note">
      <span className="ro-tag">Read only</span>
      Read-only copy of{" "}
      {origin ? (
        <a href={routeUrl(view)} onClick={(e) => follow(e, view)}>
          {readOnlyOf(origin)}
        </a>
      ) : (
        readOnlyOf(null)
      )}
      {READ_ONLY_TAIL}
    </p>
  );
}
