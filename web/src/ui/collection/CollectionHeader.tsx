// The head of a collection's Studio column: its cover, its name to edit, what
// it holds, Share, and the Studio and Ask tabs.

import { THREAD_ID } from "../chat";
import { Cover } from "../home";
import { Icon } from "../Icon";
import { follow, routeUrl, type View } from "../routes";
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
  // A copy whose author did not allow edits: read, asked and played, never
  // changed or shared. The server refuses those; the page does not offer them.
  const ro = !!summary?.read_only;
  // A read-only copy's original: its name while its share is there, as the
  // server says it; null once it is not.
  const origin = summary?.reused_from_title ?? null;
  // The studio names it from its sources, as the server says.
  const naming = !!summary?.auto_named;
  const nameNote = summary?.name_note ?? null;

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
          {nameNote !== null && (
            <>
              <span>·</span>
              <span className="sh-auto">{nameNote}</span>
            </>
          )}
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
        {ro && <ReadOnlyNote id={summary?.reused_from ?? ""} origin={origin} />}
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
function ReadOnlyNote({ id, origin }: { id: string; origin: string | null }) {
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
