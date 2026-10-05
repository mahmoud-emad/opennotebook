// What a collection's page holds, as stores rather than React state: the work
// in `actions.ts` runs for seconds or minutes and must read what is true when
// it lands, not what was true when it set out. Each part of the page reads
// only the stores it draws, so typing in one box does not redraw the rest.

import type { CollectionSummary, SessionSummary } from "../api";
import type { Estimate } from "../dialogs";
import { newMapState } from "../mindmap";
import { newNotesState } from "../notes";
import type { Open } from "../routes";
import type { Output } from "../shell";
import type { Src } from "../sources";
import { store } from "../store";
import { STYLES } from "../styles";
import { offeredLengths } from "../audioFormats";

export type CollectionPageProps = {
  cid: string;
  open: Open | null;
  /** A kind to open the Studio on, from an old address that asked for one. */
  start: Output | null;
  /** The collection's title, for the breadcrumb. */
  setCrumb: (title: string) => void;
  onOpen: (o: Open | null) => void;
  onGone: () => void;
};

/** The centre column's two tabs. */
export type Tab = "studio" | "ask";

/** Everything the page holds, as stores its long-running work reads when it
 * lands. Made once per page. */
export function pageState(props: CollectionPageProps) {
  return {
    props: store(props),
    // The collection and its decks and audio overviews.
    summary: store<CollectionSummary | null>(null),
    outputs: store<SessionSummary[]>([]),
    loaded: store(false),
    missing: store(false),
    loadErr: store(""),
    // The title field. Follows the server's name until the person types.
    title: store(""),
    typing: store(false),
    // A new name on its way to the server.
    renaming: store(false),
    // Sources.
    srcs: store<Src[]>([]),
    srcsLoaded: store(false),
    srcsErr: store(""),
    // The add box, shared by Add source and Research a topic.
    draft: store(""),
    // An Add on its way: the button waits, and a note being kept shows as a
    // row under its first words until it is a source.
    adding: store(false),
    addingNote: store(""),
    // Sources being removed, by their stored name.
    removing: store<string[]>([]),
    // Topics being researched, each shown as a row until its report lands.
    // By a number of their own, so the same topic asked twice is two rows.
    researching: store<[number, string][]>([]),
    // Files being uploaded and read, by a number of their own and their name,
    // each a row until it is a source or says why it is not.
    uploading: store<[number, string][]>([]),
    mm: newMapState(),
    nt: newNotesState(),
    // Making.
    tab: store<Tab>("studio"),
    // The tile whose options are open; null shows the tiles alone.
    chosen: store<Output | null>(props.start),
    style: store(STYLES[0]!.id),
    // An audio overview's three options: NotebookLM's format, length and
    // Customize box. Deep Dive at its default length is what NotebookLM makes
    // when nothing is chosen.
    audioFormat: store("deep_dive"),
    audioLength: store("default"),
    // The Create panel starts on the settings' style, format and length, and
    // follows them when they change, until the person picks their own here.
    picked: store(false),
    audioFocus: store(""),
    // The build call is in flight (seconds); the build itself is not waited on.
    generating: store(false),
    genErr: store(""),
    // Why the last action on one row failed, by row: `session:<id>`,
    // `map:<id>`, `notes:<id>`, `src:<file>`. Said under that row.
    rowErr: store<Record<string, string>>({}),
    // What is being done to one row right now, by the same keys: "Renaming…",
    // "Deleting…". Said on that row until it is done.
    rowBusy: store<Record<string, string>>({}),
    // A build's estimated cost, fetched whenever what would be built changes.
    est: store<Estimate | null>(null),
    estErr: store(""),
    estLoading: store(false),
    // The itemised cost dialog, from the options' Estimate cost.
    estOpen: store(false),
  };
}

export type PageState = ReturnType<typeof pageState>;

/** A length the format does not offer falls back to its default. */
export function fitLength(S: PageState): void {
  if (!offeredLengths(S.audioFormat.get()).includes(S.audioLength.get())) S.audioLength.set("default");
}

/** A source that did not arrive, kept as a row that says why until dismissed. */
export function failedSrc(name: string, why: string): Src {
  return { icon: "", name, detail: why, ok: false, url: "", file: "" };
}

/** The sources a build would read: on the server, not being read, not failed. */
export const staged = (srcs: Src[]) => srcs.filter((s) => s.ok && s.file !== "").map((s) => s.file);

/** How many sources a build would read. */
export const stagedCount = (srcs: Src[]) => staged(srcs).length;

export const sameOpen = (a: Open | null, b: Open | null) => JSON.stringify(a) === JSON.stringify(b);
