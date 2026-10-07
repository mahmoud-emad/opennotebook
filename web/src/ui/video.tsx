// Videos of outputs (docs/video-overview-spec.md): the Studio's Video
// overview tool, and a video's line on its output's row (making, ready to
// watch, or failed with why). Watching is a page of its own (`watch.tsx`).
//
// A video overview is a one-narrator deck made for its video: the server
// builds the deck and starts the video when it is ready, so in the list it
// is the deck's row, with its video under it. Any finished deck or audio
// overview can be made into a video from its row as well.

import { useCallback, useEffect, useState } from "react";
import type * as Rest from "@/client/types.gen";
import { apiBase, call, enc, errText } from "./api";
import { Icon } from "./Icon";
import { follow, routeUrl, type View } from "./routes";
import "../styles/video.css";

export type VideoStyle = "whiteboard" | "slides";
/** A whiteboard's look (the server's build/whiteboard/theme.py). */
export type VideoTheme = Rest.ThemeOut;
export type VideoLength = "short" | "default" | "long";
export type Video = Rest.VideoState;

export const STYLE_LABEL: Record<VideoStyle, string> = {
  whiteboard: "Whiteboard",
  slides: "Slides",
};
const STYLE_BLURB: Record<VideoStyle, string> = {
  whiteboard: "A presenter explains while each idea is drawn as it is said.",
  slides: "The deck's slides, each on screen while it is narrated.",
};
const LENGTH_LABEL: Record<VideoLength, string> = {
  short: "Shorter",
  default: "Default",
  long: "Longer",
};

// ── the server ───────────────────────────────────────────────────────────────

export async function makeOverview(
  cid: string,
  style: VideoStyle,
  length: VideoLength,
  theme?: string,
): Promise<Rest.OverviewOut> {
  return call<Rest.OverviewOut>("POST", `/collections/${enc(cid)}/videos`, {
    style,
    length,
    ...(theme ? { theme } : {}),
  });
}

/** The looks a whiteboard video can be made in, the default first. */
export async function videoThemes(): Promise<VideoTheme[]> {
  return call<VideoTheme[]>("GET", "/video/themes");
}

export async function videosOf(sid: string): Promise<Video[]> {
  return call<Video[]>("GET", `/sessions/${enc(sid)}/videos`);
}

export async function makeVideo(sid: string, style: VideoStyle, theme?: string): Promise<Video> {
  return call<Video>("POST", `/sessions/${enc(sid)}/video`, { style, ...(theme ? { theme } : {}) });
}

export function videoUrl(sid: string, style: VideoStyle, download = false): string {
  const q = `style=${style}${download ? "&download=true" : ""}`;
  return `${apiBase()}/sessions/${enc(sid)}/video?${q}`;
}

export function captionsUrl(sid: string, style: VideoStyle): string {
  return `${apiBase()}/sessions/${enc(sid)}/video/captions?style=${style}`;
}

/** Whether a video is still on its way: asked for, or being made. */
export const pending = (v: Video) => v.state === "waiting" || v.state === "rendering";

// ── the Studio tool ──────────────────────────────────────────────────────────

/** The Video overview tile, beside the Studio's others. */
export function VideoTile({ on, disabled, title, onClick }: {
  on: boolean;
  disabled: boolean;
  title: string;
  onClick: () => void;
}) {
  return (
    <button className={on ? "tile on" : "tile"} aria-pressed={on} disabled={disabled} title={title}
      onClick={onClick}>
      <span className="tile-i">
        <Icon name="collection-play" className="lg" />
      </span>
      <span className="tile-n">Video overview</span>
      <span className="tile-d">A presenter explains it, drawing as they go.</span>
    </button>
  );
}

/** The tool's options and its one button. The video is made in the
 * background: its deck appears in the list at once, its video under it. */
export function VideoOptions({
  cid,
  onMade,
  onCancel,
}: {
  cid: string;
  onMade: () => void;
  onCancel: () => void;
}) {
  const [style, setStyle] = useState<VideoStyle>("whiteboard");
  const [length, setLength] = useState<VideoLength>("default");
  const [making, setMaking] = useState(false);
  const [err, setErr] = useState("");
  const go = () => {
    if (making) return;
    setMaking(true);
    setErr("");
    makeOverview(cid, style, length)
      .then(() => onMade())
      .catch((e) => setErr(errText(e)))
      .finally(() => setMaking(false));
  };
  return (
    <div className="opts" role="region" aria-label="Video overview options">
      <div className="opts-h">
        <span className="opts-t">Video overview</span>
        <span className="opts-d">A few minutes · about 30 cents</span>
      </div>
      <div className="opt-l">Style</div>
      <div className="ao-formats" role="radiogroup" aria-label="Style">
        {(["whiteboard", "slides"] as const).map((s) => (
          <button key={s} className={style === s ? "ao-f on" : "ao-f"} role="radio"
            aria-checked={style === s} onClick={() => setStyle(s)}>
            <span className="ao-n">{STYLE_LABEL[s]}</span>
            <span className="ao-d">{STYLE_BLURB[s]}</span>
          </button>
        ))}
      </div>
      <div className="ao-len" role="radiogroup" aria-label="Length">
        <span className="opt-l">Length</span>
        {(["short", "default", "long"] as const).map((l) => (
          <button key={l} className={length === l ? "chip on" : "chip"} role="radio"
            aria-checked={length === l} onClick={() => setLength(l)}>
            {LENGTH_LABEL[l]}
          </button>
        ))}
      </div>
      <p className="opt-hint">
        One narrator, in the first voice of Settings › Voices. Every label on the board is checked
        against your sources before it is drawn.
      </p>
      {err !== "" && (
        <div className="opt-err" role="alert">
          {err}
        </div>
      )}
      <div className="opts-a">
        <span className="grow" />
        <button className="ghost" onClick={onCancel}>
          Cancel
        </button>
        <button className="primary" disabled={making} onClick={go}>
          {making ? "Starting…" : "Make video"}
        </button>
      </div>
    </div>
  );
}

// ── on an output's row ───────────────────────────────────────────────────────

const POLL_MS = 3000;

/** An output's videos as its row shows them: how far one being made is,
 * Watch for one that is ready, why one failed, and Make video for a
 * finished output that has none. Asks again every few seconds while one is
 * on its way. */
export function VideoLine({ sid, ready, ro }: { sid: string; ready: boolean; ro: boolean }) {
  const [videos, setVideos] = useState<Video[] | null>(null);
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState("");
  const load = useCallback(() => {
    videosOf(sid)
      .then((v) => setVideos(v))
      .catch(() => setVideos((v) => v ?? []));
  }, [sid]);
  useEffect(() => {
    load();
  }, [load, ready]);
  const live = (videos ?? []).some(pending);
  useEffect(() => {
    if (!live) return;
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [live, load]);

  if (videos === null) return null;
  const asked = videos.filter((v) => v.state !== "none");
  const start = () => {
    if (starting) return;
    setStarting(true);
    setErr("");
    makeVideo(sid, "whiteboard")
      .then(load)
      .catch((e) => setErr(errText(e)))
      .finally(() => setStarting(false));
  };
  if (asked.length === 0) {
    if (!ready || ro) return null;
    return (
      <div className="out-video">
        <button className="link-btn" disabled={starting} onClick={start}>
          <Icon name="collection-play" />
          {starting ? "Starting…" : "Make a video of this"}
        </button>
        {err !== "" && <span className="out-d bad">{err}</span>}
      </div>
    );
  }
  return (
    <>
      {asked.map((v) => (
        <div key={v.style} className="out-video">
          <Icon name="collection-play" />
          <span className="out-d num">
            {`${STYLE_LABEL[v.style as VideoStyle]} video`}
            {v.state === "waiting" && (v.waiting ? ` · ${v.waiting}` : " · starts when the deck is made")}
            {v.state === "rendering" && " · being made…"}
            {v.state === "ready" && (v.duration_ms ?? 0) > 0 && ` · ${Math.round((v.duration_ms ?? 0) / 1000)} s`}
            {v.state === "ready" && (v.claims ?? 0) > 0 && ` · ${v.supported ?? 0}/${v.claims} claims checked`}
          </span>
          {v.state === "failed" && <span className="out-d bad">{v.failure}</span>}
          {v.playable && <WatchLink sid={sid} style={v.style as VideoStyle} />}
          {v.state === "failed" && !ro && (
            <button className="link-btn" disabled={starting} onClick={start}>
              <Icon name="arrow-clockwise" />
              Try again
            </button>
          )}
        </div>
      ))}
      {err !== "" && <div className="out-video out-d bad">{err}</div>}
    </>
  );
}

// ── watching ─────────────────────────────────────────────────────────────────

/** Watch: the video's own page, with the tutor beside it. */
function WatchLink({ sid, style }: { sid: string; style: VideoStyle }) {
  const view: View = { kind: "watch", sid, style, share: null };
  return (
    <a className="link-btn" href={routeUrl(view)} onClick={(e) => follow(e, view)}>
      <Icon name="play-fill" />
      Watch
    </a>
  );
}
