// Mind maps on the collection page: making one, and the map itself, drawn as
// SVG in the viewer beside the Studio. A port of the old app's `mindmap.rs`.
//
// Where the boxes go is `mindmapLayout.ts`, tested on its own; this file only
// draws what it returns and handles the person's input. A click on a label
// asks the chat NotebookLM's own question about that topic, through `onAsk`.
// See `docs/mindmap-spec.md` section 6.

import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { errText, isAbort } from "./api";
import { sharedMindmap } from "./api-share";
import {
  mindmapCreate,
  mindmapEstimate,
  mindmapGet,
  mindmapList,
  type MindMap,
  type MindMapSummary,
} from "./api-studio";
import type { Estimate } from "./dialogs";
import { Icon } from "./Icon";
import {
  EMPTY_LAYOUT,
  FONT_PX,
  LINK_COLOUR,
  PAD_X,
  TOGGLE_W,
  allOpen,
  boundsOf,
  colours,
  cy,
  estimateWidth,
  initialOpen,
  layout,
  linkD,
  nodeAt,
  pathKey,
  questionFor,
  toMarkdown,
  toOpml,
  type Layout,
  type NodePath,
  type OpenSet,
} from "./mindmapLayout";
import { download, fileStem, saveBlob } from "./common";
import { keepSame } from "./helpers";
import { madeBy, newMaking, sorted, type MakingState } from "./making";
import { store, type Store } from "./store";

// ── making one ───────────────────────────────────────────────────────────────

/** The collection page's mind maps: the list, the options' focus, and a map
 * being made, shared by the Studio's options and its outputs list. */
export type MapState = {
  maps: Store<MindMapSummary[]>;
  focus: Store<string>;
  making: Store<boolean>;
  err: Store<string>;
  /** The list has answered once, well or not. */
  loaded: Store<boolean>;
  /** Why the list could not be read; said in the outputs list. */
  loadErr: Store<string>;
  est: Store<Estimate | null>;
  estLoading: Store<boolean>;
  /** Why the last estimate could not be had, as a sentence. */
  estErr: Store<string>;
  /** Which list read and which estimate are the latest: an older answer
   * landing after a newer one is dropped. */
  seq: { load: number; est: number };
  /** The maps being made, followed on the server. */
  mk: MakingState;
};

export function newMapState(): MapState {
  return {
    maps: store<MindMapSummary[]>([]),
    focus: store(""),
    making: store(false),
    err: store(""),
    loaded: store(false),
    loadErr: store(""),
    est: store<Estimate | null>(null),
    estLoading: store(false),
    estErr: store(""),
    seq: { load: 0, est: 0 },
    mk: newMaking(),
  };
}

/** The collection's maps, newest first. One being made is not listed: the
 * list says "Making a mind map…" while the server makes it, and reads the
 * maps again once it is done. */
export async function loadMaps(cid: string, st: MapState, signal?: AbortSignal): Promise<void> {
  const my = ++st.seq.load;
  try {
    const list = await mindmapList(cid, signal);
    if (my !== st.seq.load) return;
    const { ready, making } = sorted(
      list,
      st.mk,
      (why) => {
        if (why !== null) st.err.set(why);
        void loadMaps(cid, st, signal);
      },
      signal,
    );
    st.maps.set((was) => keepSame(was, ready, (x) => x.id));
    st.making.set(making);
    st.loadErr.set("");
  } catch (e) {
    if (isAbort(e) || my !== st.seq.load) return;
    st.loadErr.set(errText(e));
    // What the server is making is not known now; only this page's own asks.
    st.making.set(st.mk.asked > 0);
  }
  st.loaded.set(true);
}

/** What a map of the collection would cost. */
export async function estimateMap(cid: string, st: MapState, signal?: AbortSignal): Promise<void> {
  const my = ++st.seq.est;
  st.estLoading.set(true);
  st.estErr.set("");
  try {
    const e = await mindmapEstimate(cid, signal);
    if (my !== st.seq.est) return;
    st.est.set(e);
  } catch (e) {
    if (isAbort(e) || my !== st.seq.est) return;
    st.est.set(null);
    st.estErr.set(errText(e));
  }
  st.estLoading.set(false);
}

/** Make a map of the collection with the focus typed in its options, and
 * return its id to open once the server has drawn it. Null when it failed;
 * the reason is in `st.err`. */
export async function makeMap(cid: string, st: MapState): Promise<string | null> {
  if (st.making.get()) return null;
  st.mk.asked++;
  st.making.set(true);
  st.err.set("");
  const f = st.focus.get().trim();
  let out: { id: string } | { why: string };
  try {
    out = await madeBy(st.mk, async () => {
      const started = await mindmapCreate(cid, f);
      st.focus.set("");
      return started;
    });
  } catch (e) {
    out = { why: errText(e) };
  }
  st.mk.asked--;
  // Read back as the newest list: it says whether anything is still being made.
  await loadMaps(cid, st);
  st.loaded.set(true);
  if ("why" in out) {
    st.err.set(out.why);
    return null;
  }
  return out.id;
}

/** The newest map made from exactly `sources`, if one is: the map that is
 * already up to date, so making another would draw the same tree again.
 * Order does not matter; a source added or removed since does. */
export function coveringMap(maps: MindMapSummary[], sources: string[]): string | null {
  return covering(maps, sources);
}

/** The newest of `list` with no focus made from exactly `sources`. */
export function covering<T extends { id: string; focus: string; sources: string[] }>(
  list: T[],
  sources: string[],
): string | null {
  if (sources.length === 0) return null;
  const want = new Set(sources);
  const same = (s: string[]) => {
    const have = new Set(s);
    return have.size === want.size && [...have].every((x) => want.has(x));
  };
  return list.find((m) => m.focus.trim() === "" && same(m.sources))?.id ?? null;
}

// ── the map ──────────────────────────────────────────────────────────────────

/** The pan and zoom of the drawing: translate, then scale. */
type View = { tx: number; ty: number; k: number };

const K_MIN = 0.1;
const K_MAX = 50;
/** Room left round a fitted map, in px. */
const FIT_PAD = 24;

let measureCtx: CanvasRenderingContext2D | null | undefined;

/** A label's width as the browser draws it, from a canvas set to the font the
 * map uses. Falls back to the estimate where there is no canvas. */
function measure(label: string): number {
  if (measureCtx === undefined) {
    try {
      measureCtx = document.createElement("canvas").getContext("2d");
      if (measureCtx) measureCtx.font = `500 ${FONT_PX}px system-ui, sans-serif`;
    } catch {
      measureCtx = null;
    }
  }
  return measureCtx ? measureCtx.measureText(label).width : estimateWidth(label);
}

/** The drawing pane's position on screen and its size. */
function pane(): [number, number, number, number] | null {
  const el = document.getElementById("mm-canvas");
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return [r.left, r.top, r.width, r.height];
}

/** The view that shows the box (x, y, w, h) whole and centred, no larger than
 * life-size times 1.25, so a small map is not blown up. */
function fitTo([x, y, w, h]: [number, number, number, number]): View | null {
  const p = pane();
  if (!p) return null;
  const [, , pw, ph] = p;
  const k = Math.min(Math.max(Math.min((pw - 2 * FIT_PAD) / Math.max(w, 1), (ph - 2 * FIT_PAD) / Math.max(h, 1)), K_MIN), 1.25);
  return { tx: (pw - w * k) / 2 - x * k, ty: (ph - h * k) / 2 - y * k, k };
}

/** `v` zoomed by `factor`, keeping the point under (cx, cy) where it is. */
function zoomed(v: View, factor: number, at: [number, number] | null): View {
  const k = Math.min(Math.max(v.k * factor, K_MIN), K_MAX);
  const p = pane();
  const [cx, cy] = at ?? (p ? [p[2] / 2, p[3] / 2] : [0, 0]);
  return { tx: cx - ((cx - v.tx) * k) / v.k, ty: cy - ((cy - v.ty) * k) / v.k, k };
}

const domId = (path: NodePath) => ["mm-n", ...path].join("-");
const samePath = (a: NodePath, b: NodePath) => pathKey(a) === pathKey(b) && a.length === b.length;

/** A map, drawn. `onAsk` gets the question a click asks. Keyed on the map by
 * its caller, so another map starts from nothing.
 *
 * Read only, it is someone else's map on a shared collection's page: there
 * is no chat to ask, so a click on a topic opens or closes it instead. */
export function MindMapView({
  cid,
  id,
  title,
  onClose,
  onAsk,
  readOnly = false,
  shareId,
}: {
  cid: string;
  id: string;
  /** The share it is read through, on a shared collection's page: someone
   * else's map is read there, not from their collection. */
  shareId?: string;
  /** Its name as the outputs list has it, which a rename changes while the
   * map is open. Empty until the list has loaded. */
  title: string;
  onClose: () => void;
  onAsk?: (q: string) => void;
  readOnly?: boolean;
}) {
  const [map, setMap] = useState<MindMap | null>(null);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState<OpenSet>(initialOpen);
  const [view, setView] = useState<View>({ tx: FIT_PAD, ty: FIT_PAD, k: 1 });
  const [focus, setFocus] = useState<NodePath>([]);
  // Pointer down: where, and the view then. Moved: it became a drag, so the
  // click that ends it is not a click on a node.
  const drag = useRef<[number, number, View] | null>(null);
  const moved = useRef(false);
  const [dragging, setDragging] = useState(false);
  const [menu, setMenu] = useState(false);
  const svg = useRef<SVGSVGElement>(null);

  const l = useMemo(() => (map ? layout(map.root, open, measure) : EMPTY_LAYOUT), [map, open]);

  // Load, then fit the whole map once the pane has drawn.
  useEffect(() => {
    let live = true;
    let t: ReturnType<typeof setTimeout> | undefined;
    const ctrl = new AbortController();
    (shareId !== undefined ? sharedMindmap(shareId, id, ctrl.signal) : mindmapGet(cid, id, ctrl.signal)).then(
      (m) => {
        if (!live) return;
        const first = layout(m.root, initialOpen(), measure);
        setMap(m);
        // Fit once the pane has drawn and has a size.
        t = setTimeout(() => {
          const v = fitTo([0, 0, first.width, first.height]);
          if (v) setView(v);
        }, 0);
      },
      (e) => live && !isAbort(e) && setErr(errText(e)),
    );
    return () => {
      live = false;
      clearTimeout(t);
      ctrl.abort();
    };
  }, [cid, id, shareId]);

  // The wheel zooms the map rather than the page, which a passive listener
  // (React's) cannot prevent.
  useEffect(() => {
    const el = svg.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const p = pane();
      const at: [number, number] | null = p ? [e.clientX - p[0], e.clientY - p[1]] : null;
      setView((v) => zoomed(v, e.deltaY < 0 ? 1.1 : 1 / 1.1, at));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  // Open or close a branch.
  const toggle = (path: NodePath) =>
    setOpen((o) => {
      const n = new Set(o);
      if (!n.delete(pathKey(path))) n.add(pathKey(path));
      return n;
    });

  // Ask about a node: NotebookLM's question to the chat, the node opened if
  // it was closed, and the view moved to it and its children.
  const ask = (path: NodePath) => {
    if (!map) return;
    const q = questionFor(map.root, path);
    if (q === null) return;
    setFocus(path);
    const branch = (nodeAt(map.root, path)?.children?.length ?? 0) > 0;
    if (readOnly) {
      // Nothing to ask: the topic opens or closes, as its toggle does.
      if (branch) toggle(path);
      return;
    }
    let next = open;
    if (branch) {
      next = new Set(open).add(pathKey(path));
      setOpen(next);
    }
    const b = boundsOf(layout(map.root, next, measure), path);
    const v = b && fitTo(b);
    if (v) setView(v);
    onAsk?.(q);
  };

  const onKey = (e: KeyboardEvent) => {
    if (!map) return;
    const found = l.nodes.findIndex((n) => samePath(n.path, focus));
    const at = found < 0 ? 0 : found;
    const node = l.nodes[at];
    const go = (i: number) => {
      const n = l.nodes[i];
      if (n) setFocus(n.path);
    };
    const last = Math.max(l.nodes.length - 1, 0);
    switch (e.key) {
      case "ArrowDown":
        go(Math.min(at + 1, last));
        break;
      case "ArrowUp":
        go(Math.max(at - 1, 0));
        break;
      case "Home":
        go(0);
        break;
      case "End":
        go(last);
        break;
      case "ArrowRight":
        if (node && node.children > 0 && !node.open) setOpen((o) => new Set(o).add(pathKey(node.path)));
        else if (node && node.children > 0) setFocus([...node.path, 0]);
        break;
      case "ArrowLeft":
        if (node?.open)
          setOpen((o) => {
            const n = new Set(o);
            n.delete(pathKey(node.path));
            return n;
          });
        else if (node && node.path.length > 0) setFocus(node.path.slice(0, -1));
        break;
      case "Enter":
        ask(focus);
        break;
      case " ":
        if ((nodeAt(map.root, focus)?.children?.length ?? 0) > 0) toggle(focus);
        break;
      default:
        return;
    }
    e.preventDefault();
  };

  // The list's name for it first: the row and the frame around the map then
  // always say the same, a rename included, before and after it loads.
  const shown = title.trim() === "" ? (map?.title ?? "") : title;
  let note: string | null = null;
  if (map) {
    const parts = [`${map.node_count} topics`];
    if (map.dropped > 0) parts.push(`${map.dropped} left out, not found in the sources`);
    if (map.excerpted) parts.push("built from excerpts of long sources");
    if (map.focus !== "") parts.push(`focus: ${map.focus}`);
    note = parts.join(" · ");
  }

  return (
    <div className="mm-view">
      <div className="mm-bar">
        <div className="mm-title">
          <div className="mm-h">{shown}</div>
          {note !== null && (
            <div className="mm-note">
              {readOnly && <span className="ro-tag">Read only</span>}
              {note}
            </div>
          )}
        </div>
        <div className="mm-tools" role="toolbar" aria-label="Mind map">
          <button
            className="icon-btn"
            title="Expand all"
            aria-label="Expand all"
            onClick={() => map && setOpen(allOpen(map.root))}
          >
            <Icon name="arrows-expand" />
          </button>
          <button className="icon-btn" title="Collapse all" aria-label="Collapse all" onClick={() => setOpen(initialOpen())}>
            <Icon name="arrows-collapse" />
          </button>
          <button className="icon-btn" title="Zoom in" aria-label="Zoom in" onClick={() => setView((v) => zoomed(v, 1.2, null))}>
            <Icon name="zoom-in" />
          </button>
          <button
            className="icon-btn"
            title="Zoom out"
            aria-label="Zoom out"
            onClick={() => setView((v) => zoomed(v, 0.8, null))}
          >
            <Icon name="zoom-out" />
          </button>
          <button
            className="icon-btn"
            title="Fit to the pane"
            aria-label="Fit to the pane"
            onClick={() => {
              const v = fitTo([0, 0, l.width, l.height]);
              if (v) setView(v);
            }}
          >
            <Icon name="arrows-fullscreen" />
          </button>
          <div className="mm-menu-wrap">
            <button
              className="icon-btn"
              title="Download"
              aria-label="Download"
              aria-expanded={menu}
              onClick={() => setMenu(!menu)}
            >
              <Icon name="download" />
            </button>
            {menu && (
              <div className="mm-menu" role="menu">
                <button
                  role="menuitem"
                  onClick={() => {
                    setMenu(false);
                    exportPng(l, shown);
                  }}
                >
                  Image (PNG)
                </button>
                <button
                  role="menuitem"
                  onClick={() => {
                    setMenu(false);
                    if (map) download(`${fileStem(shown)}.md`, "text/markdown", toMarkdown(map.root));
                  }}
                >
                  Outline (Markdown)
                </button>
                <button
                  role="menuitem"
                  onClick={() => {
                    setMenu(false);
                    if (map) download(`${fileStem(shown)}.opml`, "text/x-opml", toOpml(map.root));
                  }}
                >
                  Outline (OPML)
                </button>
              </div>
            )}
          </div>
          <button className="icon-btn" title="Close the map" aria-label="Close the map" onClick={onClose}>
            <Icon name="x-lg" />
          </button>
        </div>
      </div>
      <div id="mm-canvas" className={dragging ? "mm-canvas dragging" : "mm-canvas"}>
        {err !== "" ? (
          <div className="mm-msg bad" role="alert">
            {err}
          </div>
        ) : (
          !map && (
            <div className="mm-msg">
              <span className="mini-spin" /> Opening the map…
            </div>
          )
        )}
        <svg
          ref={svg}
          id="mm-svg"
          width="100%"
          height="100%"
          tabIndex={0}
          role="tree"
          aria-label={
            readOnly
              ? `Mind map: ${shown}. Arrow keys move, Enter or Space opens or closes a topic.`
              : `Mind map: ${shown}. Arrow keys move, Enter asks about a topic, Space opens or closes it.`
          }
          aria-activedescendant={domId(focus)}
          onKeyDown={onKey}
          onPointerDown={(e: PointerEvent) => {
            drag.current = [e.clientX, e.clientY, view];
            moved.current = false;
            setDragging(true);
          }}
          onPointerMove={(e: PointerEvent) => {
            const d = drag.current;
            if (!d) return;
            const [x0, y0, v0] = d;
            const dx = e.clientX - x0;
            const dy = e.clientY - y0;
            if (Math.abs(dx) + Math.abs(dy) > 4) moved.current = true;
            if (moved.current) setView({ tx: v0.tx + dx, ty: v0.ty + dy, k: v0.k });
          }}
          onPointerUp={() => {
            drag.current = null;
            setDragging(false);
          }}
          onPointerLeave={() => {
            drag.current = null;
            setDragging(false);
          }}
        >
          <g className="mm-root" style={{ transform: `translate(${view.tx}px, ${view.ty}px) scale(${view.k})` }}>
            {l.links.map((link) => (
              <path
                key={`l${domId(link.to)}`}
                d={linkD(link)}
                style={{ fill: "none", stroke: LINK_COLOUR, strokeWidth: 1.5 }}
              />
            ))}
            {l.nodes.map((n) => {
              const [fill, ink] = colours(n.depth);
              const isFocus = samePath(focus, n.path);
              const ty = cy(n);
              const tx = n.x + PAD_X;
              const label = n.children > 0 ? `${n.name}, ${n.children} subtopics` : n.name;
              const cx = n.x + n.w - TOGGLE_W / 2 - 4;
              return (
                <g
                  key={domId(n.path)}
                  id={domId(n.path)}
                  role="treeitem"
                  aria-level={n.depth + 1}
                  aria-expanded={n.children > 0 ? n.open : undefined}
                  aria-selected={isFocus}
                  aria-label={label}
                  className="mm-node"
                >
                  <rect
                    x={n.x}
                    y={n.y}
                    width={n.w}
                    height={n.h}
                    rx="8"
                    style={
                      isFocus ? { fill, stroke: "var(--st-focus)", strokeWidth: 2.5 } : { fill, stroke: "none" }
                    }
                    onClick={() => !moved.current && ask(n.path)}
                  />
                  <text
                    x={tx}
                    y={ty}
                    dominantBaseline="central"
                    style={{ fill: ink }}
                    fontSize={FONT_PX}
                    fontWeight="500"
                    fontFamily="system-ui, sans-serif"
                    pointerEvents="none"
                  >
                    {n.name}
                  </text>
                  {n.children > 0 && (
                    <>
                      <circle
                        cx={cx}
                        cy={ty}
                        r="8"
                        style={{ fill: ink, fillOpacity: 0.16 }}
                        className="mm-tog"
                        onClick={() => !moved.current && toggle(n.path)}
                      />
                      <text
                        x={cx}
                        y={ty}
                        dominantBaseline="central"
                        textAnchor="middle"
                        style={{ fill: ink }}
                        fontSize="12"
                        fontFamily="system-ui, sans-serif"
                        pointerEvents="none"
                      >
                        {n.open ? "‹" : "›"}
                      </text>
                    </>
                  )}
                </g>
              );
            })}
          </g>
        </svg>
      </div>
    </div>
  );
}

/** The drawn map as a PNG at twice its size, on the page's background.
 *
 * The live map takes its colours from the theme's CSS variables, which mean
 * nothing to an SVG drawn as an image. So every shape of the copy is given
 * the colours the browser actually computed for it, and the background is
 * the page's own: a PNG made in light mode is a light map. Only the pan and
 * zoom are taken off. */
function exportPng(l: Layout, title: string): void {
  const pad = 24;
  const w = l.width + 2 * pad;
  const h = l.height + 2 * pad;
  const src = document.getElementById("mm-svg");
  if (!src) return;
  const svg = src.cloneNode(true) as Element;
  svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  svg.setAttribute("width", String(w));
  svg.setAttribute("height", String(h));
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const o = src.querySelectorAll("path,rect,text,circle");
  const k = svg.querySelectorAll("path,rect,text,circle");
  o.forEach((e, i) => {
    const cs = getComputedStyle(e);
    k[i]?.setAttribute(
      "style",
      `fill:${cs.fill};fill-opacity:${cs.fillOpacity};stroke:${cs.stroke};stroke-width:${cs.strokeWidth}`,
    );
  });
  const g = svg.querySelector(".mm-root");
  g?.removeAttribute("style");
  g?.setAttribute("transform", `translate(${pad},${pad})`);
  const bg = getComputedStyle(document.body).backgroundColor;
  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(new XMLSerializer().serializeToString(svg))}`;
  const img = new Image();
  img.onload = () => {
    const c = document.createElement("canvas");
    c.width = w * 2;
    c.height = h * 2;
    const x = c.getContext("2d");
    if (!x) return;
    x.fillStyle = bg;
    x.fillRect(0, 0, c.width, c.height);
    x.scale(2, 2);
    x.drawImage(img, 0, 0);
    c.toBlob((b) => b && saveBlob(b, `${fileStem(title)}.png`), "image/png");
  };
  img.src = url;
}
