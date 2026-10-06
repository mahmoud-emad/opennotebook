// A collection's sources as the sources panel shows them: one row each, led by
// the icon of where it came from. A port of the source half of the old app's
// `main.rs` (`Src`, `SrcRow`, `SrcIcon`, `server_sources`, `src_from`).

import { useState } from "react";
import { sourceList, type ServerSource } from "./api";
import { str } from "./helpers";
import { Icon } from "./Icon";
import { RowErr } from "./common";

/** One source on the sources panel. */
export type Src = {
  /** What it is called: the page's title, or a note's first words. */
  name: string;
  detail: string;
  ok: boolean;
  /** The page it came from; empty for a typed note. Drives the row's icon. */
  url: string;
  /** The icon the page declares for itself, found by the server while it
   * read the page. Empty until then, or when there was no page. */
  icon: string;
  /** Its stored file name on the server, what a remove takes. Empty for a
   * row that is not on the server: one being read, or one that failed. */
  file: string;
  /** What a row not on the server is known by while it is shown, since it has
   * no stored name. Unset for a row the server listed. */
  key?: string;
};

/** The row a source shows while its page is still being read. */
export const FETCHING = "fetching…";

/** What a source without a page is, from its stored name: a typed note or a
 * report is Markdown; an uploaded file says its own type, "PDF", "DOCX". */
export function fileKind(name: string): string {
  const dot = name.lastIndexOf(".");
  const e = dot >= 0 ? name.slice(dot + 1).toLowerCase() : null;
  return e !== null && e !== "md" && e !== "" ? e.toUpperCase() : "note";
}

/** `scheme://host` of a link, the root a favicon is served from. */
export function origin(url: string): string {
  const i = url.indexOf("://");
  if (i < 0) return url;
  return `${url.slice(0, i)}://${url.slice(i + 3).split("/")[0]}`;
}

export function shortHost(url: string): string {
  const i = url.indexOf("://");
  const rest = i >= 0 ? url.slice(i + 3) : url;
  return (rest.split("/")[0] ?? rest).replace(/^(www\.)+/, "");
}

/** A source the server holds, as the row the sources panel shows: the line
 * under its name and its icon as the server words them. */
export function srcOfServer(s: ServerSource): Src {
  return { detail: s.detail, name: s.title || s.name, file: s.name, ok: true, url: s.url, icon: s.icon };
}

/** What the server holds as a collection's sources, as the rows the sources
 * panel shows. Throws the reason it could not be read. */
export async function serverSources(cid: string, signal?: AbortSignal): Promise<Src[]> {
  return (await sourceList(cid, signal)).map(srcOfServer);
}

/** What a row is known by on the panel: its own key while it is not on the
 * server, its stored name once it is. */
export const srcKey = (s: Src) => s.key ?? `file:${s.file}`;

/** A source row from the server's `Fetched` shape, or from a source the chat
 * agent read. */
const UNREAD = "It could not be read. Try a direct link, or paste the text in.";

export function srcFrom(g: Record<string, unknown>): Src {
  // A source the agent read may come as the stored source itself, which has
  // no `ok`: it is there, so it arrived.
  const ok = typeof g.ok === "boolean" ? g.ok : "name" in g && !("error" in g);
  const url = str(g.url);
  const name = str(g.title);
  return {
    icon: str(g.icon),
    name: name || url,
    // The line under its name as the server words it, or why it was not read.
    detail: str(g.detail) || str(g.error) || UNREAD,
    ok,
    url,
    // The stored name, when the reply carries it; the list read back from
    // the server after an add fills it in either way.
    file: str(g.name),
  };
}

/** One source on the sources panel, led by the icon of where it came from.
 *
 * A link shows the icon its page declares (the server reads it off the page
 * while fetching it), loaded from the site directly rather than through a
 * third-party favicon service, which would learn every page a person reads.
 * While the page is still being read the row shows a spinner; if the page
 * declared no icon, `/favicon.ico` stands in; a site that has neither falls
 * back to its initial, and a typed note gets a note mark, so every row still
 * has an icon.
 *
 * The remove button takes it out of the collection, or clears a row that
 * failed and never reached the server. */
export function SrcRow({
  s,
  onRemove,
  busy = false,
  err = "",
  onDismissErr,
}: {
  s: Src;
  onRemove?: (s: Src) => void;
  /** Being removed: the row says so and offers nothing until it is gone. */
  busy?: boolean;
  /** Why the last removal failed, said under the row; empty when none did. */
  err?: string;
  onDismissErr?: () => void;
}) {
  const reading = s.detail === FETCHING;
  // Without `onRemove` the row is read only: a shared source, or one in a
  // read-only copy.
  const removable = onRemove !== undefined && !reading && !busy && (s.file !== "" || !s.ok);
  // A row that never reached the server is only dismissed, like any other
  // error row; one that did is removed from the collection.
  const verb = s.file === "" ? "Dismiss" : "Remove";
  return (
    <>
      <div className={busy ? "src run" : s.ok ? "src" : "src bad"}>
        {busy ? <span className="src-i spin" title="Removing…" /> : <SrcIcon s={s} />}
        <div className="src-t">
          <div className="src-n" title={s.name}>
            {s.name}
          </div>
          <div className="src-d">{busy ? "Removing…" : s.detail}</div>
        </div>
        {removable && (
          <button className="icon-btn src-x" title={verb} aria-label={`${verb} ${s.name}`} onClick={() => onRemove?.(s)}>
            <Icon name="x-lg" />
          </button>
        )}
      </div>
      {err !== "" && <RowErr text={err} onDismiss={() => onDismissErr?.()} />}
    </>
  );
}

/** The leading icon of a source: a spinner while it is read, then its logo. */
function SrcIcon({ s }: { s: Src }) {
  // The icon that would not load, so a row given another one tries it.
  const [broken, setBroken] = useState("");
  const host = shortHost(s.url);
  if (s.detail === FETCHING) return <span className="src-i spin" title="Reading the page…" />;
  if (s.url === "")
    return (
      <span className="src-i note">
        <Icon name="file-earmark-text" />
      </span>
    );
  const fav = s.icon === "" ? `${origin(s.url)}/favicon.ico` : s.icon;
  if (broken === fav || !s.ok) {
    const first = ([...host][0] ?? "?").toUpperCase();
    return <span className="src-i">{first}</span>;
  }
  return <img className="src-i" src={fav} alt="" onError={() => setBroken(fav)} />;
}
