// Which screen is showing, and its address. A port of the old `routes.rs`.
//
// The screen lives in the URL, so a refresh lands where you were and the
// browser's Back button walks the screens. A route is whatever follows `/ui`.
//
// The addresses from before collections still work: `new-session` and its
// siblings make a collection and open it on that kind, and a map's or notes'
// own `mind-map/<sid>/<id>` names the same screen its `c/<sid>/…` form does,
// so the address bar is quietly corrected to that form. So is `collections`,
// the list's address before Discover took the root: it names My collections.

import type { MouseEvent } from "react";
import { serviceRoot } from "./api";
import type { Output } from "./shell";

/** What is open in the viewer beside a collection's Studio. */
export type Open = { kind: "map"; id: string } | { kind: "notes"; id: string };

/** Which screen: four screens and the player, which is a page of its own. */
export type View =
  /** Discover, the root: every collection shared on this studio. */
  | { kind: "discover" }
  /** My collections: every collection of yours, and the way to start one. */
  | { kind: "mine" }
  /** One shared collection as a visitor sees it, by its share id. */
  | { kind: "shared"; id: string }
  /** One collection, with a map or notes open beside it or not. */
  | { kind: "collection"; cid: string; open: Open | null }
  /** An address from before collections that asked for a new piece of work. */
  | { kind: "new"; output: Output }
  /** The player, a page of its own: an output of yours, or one a share
   * includes (`?share=<share_id>`). */
  | { kind: "play"; sid: string; share: string | null }
  /** A video's watch page, with the tutor beside it: an output's video in
   * one style (`?style=`), of yours or one a share includes. */
  | { kind: "watch"; sid: string; style: "whiteboard" | "slides"; share: string | null };

/** The path segment of the page listing every collection of yours. */
export const MY_COLLECTIONS_ROUTE = "my-collections";
/** That page's address before Discover took the root; it still lands there. */
export const LEGACY_COLLECTIONS_ROUTE = "collections";
/** The path segment of one shared collection as a visitor sees it:
 * shared/<share_id>. */
export const SHARED_ROUTE = "shared";
export const MIND_MAP_ROUTE = "mind-map";
export const STUDY_NOTES_ROUTE = "study-notes";
/** The path segment of the player: play/<sid>. */
export const PLAY_ROUTE = "play";
/** The path segment of a video's watch page: watch/<sid>?style=<style>. */
export const WATCH_ROUTE = "watch";

export function routeFromLocation(): View {
  const path = window.location.pathname;
  const i = path.indexOf("/ui");
  return i >= 0 ? parseRoute(path.slice(i + 3), window.location.search) : { kind: "discover" };
}

const ok = (s: string | undefined): s is string => !!s && /^[A-Za-z0-9_-]+$/.test(s);

function item(kind: string, id: string | undefined): Open | null {
  if (!ok(id)) return null;
  if (kind === MIND_MAP_ROUTE) return { kind: "map", id };
  if (kind === STUDY_NOTES_ROUTE) return { kind: "notes", id };
  return null;
}

/** The screen the part of the path after `/ui` names, and its query. */
export function parseRoute(rest: string, search = ""): View {
  const parts = rest.replace(/^\/+|\/+$/g, "").split("/");
  const [a, b, c, d] = parts;
  if (a === PLAY_ROUTE && parts.length === 2 && ok(b)) {
    const share = new URLSearchParams(search).get("share") ?? "";
    return { kind: "play", sid: b, share: ok(share) ? share : null };
  }
  if (a === WATCH_ROUTE && parts.length === 2 && ok(b)) {
    const q = new URLSearchParams(search);
    const share = q.get("share") ?? "";
    const style = q.get("style") === "slides" ? "slides" : "whiteboard";
    return { kind: "watch", sid: b, style, share: ok(share) ? share : null };
  }
  if (parts.length === 1 && a === "") return { kind: "discover" };
  if (parts.length === 1 && a === MY_COLLECTIONS_ROUTE) return { kind: "mine" };
  // Before Discover: the list of every collection.
  if (parts.length === 1 && a === LEGACY_COLLECTIONS_ROUTE) return { kind: "mine" };
  if (a === SHARED_ROUTE && parts.length === 2 && ok(b)) return { kind: "shared", id: b };
  if (a === "c" && parts.length === 2 && ok(b)) return { kind: "collection", cid: b, open: null };
  if (a === "c" && parts.length === 4 && ok(b)) return { kind: "collection", cid: b, open: item(c!, d) };
  // Before collections: a map's or notes' own address, on its sid.
  if ((a === MIND_MAP_ROUTE || a === STUDY_NOTES_ROUTE) && parts.length === 3 && ok(b))
    return { kind: "collection", cid: b, open: item(a, c) };
  const legacy: Record<string, Output> = {
    "new-session": "session",
    "new-mind-map": "mindmap",
    "new-study-notes": "notes",
    "new-audio-overview": "audio",
  };
  if (parts.length === 1 && a && legacy[a]) return { kind: "new", output: legacy[a] };
  return { kind: "discover" };
}

/** The address of a screen, under the bundle's mount. */
export function routeUrl(v: View): string {
  return `${serviceRoot()}${routePath(v)}`;
}

/** {@link routeUrl} after the mount: the part a screen decides for itself. */
export function routePath(v: View): string {
  switch (v.kind) {
    case "discover":
    case "new":
      return "/ui/";
    case "mine":
      return `/ui/${MY_COLLECTIONS_ROUTE}`;
    case "shared":
      return `/ui/${SHARED_ROUTE}/${v.id}`;
    case "collection":
      if (!v.open) return `/ui/c/${v.cid}`;
      return `/ui/c/${v.cid}/${v.open.kind === "map" ? MIND_MAP_ROUTE : STUDY_NOTES_ROUTE}/${v.open.id}`;
    case "play":
      return `/ui/${PLAY_ROUTE}/${v.sid}${v.share ? `?share=${v.share}` : ""}`;
    case "watch":
      return `/ui/${WATCH_ROUTE}/${v.sid}?style=${v.style}${v.share ? `&share=${v.share}` : ""}`;
  }
}

/** Put `view` in the address bar without reloading. `replace` swaps the
 * current entry instead of pushing one. */
export function setRoute(v: View, replace: boolean): void {
  const url = routeUrl(v);
  if (replace) history.replaceState(null, "", url);
  else history.pushState(null, "", url);
}

export function sameView(a: View, b: View): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** Every screen can move the app to another: the App's change of screen,
 * handed here so a card or a page need not thread a callback up to it. */
let nav: ((v: View) => void) | null = null;

/** The App hands over how it changes screen; `null` when it goes. */
export function setNav(fn: ((v: View) => void) | null): void {
  nav = fn;
}

/** Show `view`. The App's effect puts it in the address bar. */
export function go(v: View): void {
  nav?.(v);
}

/** A link's click, taken over so the screen changes without a reload. A
 * modified click (a new tab or window) is left to the browser, which follows
 * the link's real address. */
export function follow(e: MouseEvent, v: View): void {
  if (e.ctrlKey || e.metaKey || e.shiftKey) return;
  e.preventDefault();
  go(v);
}
