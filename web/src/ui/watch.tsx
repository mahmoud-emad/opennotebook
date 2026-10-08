// The watch page of a video (docs/video-overview-spec.md, the watch page):
// the video on the left, and beside it a tutor that knows the moment.
//
// The video leads, with its controls under it (`watch/Controls.tsx`). The
// rail has three tabs:
// - Explain: ask about what is on now. The quick prompts ask the usual
//   things in one press; a question pauses the video, and Resume carries on.
//   An answer cites the sources as the chat does, and its [m:ss] moments are
//   links that seek the video there. The thread is kept in this browser.
// - Transcript: the narration, following the voice; a line plays from there.
// - Chapters: the chapters and, for a whiteboard, its scenes, with times.
// A shared video has no Explain: the tutor answers from its owner's sources.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { errText } from "./api";
import { Icon } from "./Icon";
import { go } from "./routes";
import { type VideoStyle, videoName } from "./video";
import { mediaUrl, scriptOf, type Mode, type Script } from "./watch/api";
import { Captions, useCaptions } from "./watch/Captions";
import { Chapters } from "./watch/Chapters";
import { Controls, nextSpeed, useFullScreen } from "./watch/Controls";
import { Explain } from "./watch/Explain";
import { clock, onAt } from "./watch/time";
import { Transcript } from "./watch/Transcript";
import { useTutor } from "./watch/tutor";
import "../styles/watch.css";

export type { Script } from "./watch/api";
export { phrases, wordsOf } from "./watch/Captions";
export { withMoments } from "./watch/Explain";
export { clock, onAt } from "./watch/time";

type Tab = "explain" | "transcript" | "chapters";

/** Play, quietly: a browser that will not play without a press says so by
 * rejecting, and some return nothing at all. */
function playIt(v: HTMLVideoElement | null): void {
  try {
    void v?.play()?.catch(() => {});
  } catch {
    // Nothing to play with.
  }
}

export function WatchPage({ sid, style, share }: { sid: string; style: VideoStyle; share: string | null }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  // Full screen is the frame around the video, with its captions, as the
  // slides' player puts its stage in full screen.
  const frameRef = useRef<HTMLDivElement>(null);
  const [script, setScript] = useState<Script | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [captions, setCaptions] = useCaptions();
  const [full, toggleFull] = useFullScreen(frameRef);
  const [tab, setTab] = useState<Tab>(share ? "transcript" : "explain");
  const tutor = useTutor(sid, style);
  // The video was playing when a question paused it: Resume is offered.
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    document.body.classList.add("watch-open");
    return () => document.body.classList.remove("watch-open");
  }, []);
  useEffect(() => {
    let live = true;
    scriptOf(sid, style, share)
      .then((s) => live && setScript(s))
      .catch((e) => live && setLoadErr(errText(e)));
    return () => {
      live = false;
    };
  }, [sid, style, share]);

  const duration = script?.duration_ms ?? 0;
  const chapters = useMemo(() => script?.chapters ?? [], [script]);
  const scenes = script?.scenes ?? [];
  const chapterOn = onAt(chapters, t);
  const where = scenes[onAt(scenes, t)]?.title || chapters[chapterOn]?.title || "";

  const togglePlay = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) playIt(v);
    else v.pause();
  }, []);
  const cycleSpeed = () => {
    const next = nextSpeed(speed);
    setSpeed(next);
    if (videoRef.current) videoRef.current.playbackRate = next;
  };
  const seek = useCallback((ms: number, play = true) => {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = ms / 1000;
    setT(ms);
    if (play) playIt(v);
  }, []);
  const step = useCallback(
    (d: number) => {
      const to = chapters[Math.min(Math.max(chapterOn + d, 0), chapters.length - 1)];
      if (to) seek(to.start_ms);
    },
    [chapters, chapterOn, seek],
  );

  const ask = (mode: Mode, question: string, says: string) => {
    if (tutor.asking || !script) return;
    const v = videoRef.current;
    const at = Math.round((v?.currentTime ?? t / 1000) * 1000);
    if (v && !v.paused) {
      v.pause();
      setPaused(true);
    }
    tutor.ask(mode, question, says, at);
  };

  // The slides' player's keys: K or Space plays and pauses, the arrows go a
  // chapter back or on, C turns the captions on and off, F is full screen.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT" || el.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === "c") setCaptions(!captions);
      else if (k === "f") toggleFull();
      else if (k === "k" || (k === " " && el?.tagName !== "BUTTON")) {
        e.preventDefault();
        togglePlay();
      } else if (k === "arrowleft") step(-1);
      else if (k === "arrowright") step(1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [captions, setCaptions, toggleFull, togglePlay, step]);

  const back = () => {
    if (window.history.length > 1) window.history.back();
    else go({ kind: "mine" });
  };
  const readTime = (v: HTMLVideoElement) => setT(Math.round(v.currentTime * 1000));

  return (
    <div className="watch-page">
      <header className="w-top">
        <button className="w-back" title="Back" aria-label="Back" onClick={back}>
          <Icon name="chevron-left" />
        </button>
        <div className="w-titles">
          <div className="w-title">{script?.title ?? ""}</div>
          <div className="w-sub">
            {videoName({ style, theme: script?.theme })}
            {duration > 0 && ` · ${clock(duration)}`}
            {chapters.length > 0 && ` · ${chapters.length} chapters`}
          </div>
        </div>
        <a className="w-icon" href={mediaUrl(sid, style, share, true)} title="Download" aria-label="Download">
          <Icon name="download" />
        </a>
      </header>

      <main className="w-left">
        <div className="w-stage">
          <div className="w-frame" ref={frameRef}>
            <video
              ref={videoRef}
              className="w-video"
              preload="metadata"
              onClick={togglePlay}
              crossOrigin="use-credentials"
              src={mediaUrl(sid, style, share)}
              onTimeUpdate={(e) => readTime(e.currentTarget)}
              onSeeked={(e) => readTime(e.currentTarget)}
              onPlay={() => {
                setPlaying(true);
                setPaused(false);
              }}
              onPause={() => setPlaying(false)}
            />
            {captions && script && <Captions lines={script.lines} videoRef={videoRef} />}
            {!playing && t === 0 && (
              // Centred by its layer, not by a transform of its own: the
              // app's buttons move a little when pressed, and one positioned
              // by a transform jumped out from under the pointer.
              <div className="w-start-layer">
                <button className="w-start" aria-label="Play" title="Play (K)" onClick={togglePlay}>
                  <Icon name="play-fill" />
                </button>
              </div>
            )}
          </div>
        </div>
        <Controls
          chapters={chapters} duration={duration} t={t} on={chapterOn} seek={seek}
          playing={playing} togglePlay={togglePlay} step={step}
          speed={speed} cycleSpeed={cycleSpeed} captions={captions} setCaptions={setCaptions}
          full={full} toggleFull={toggleFull}
        />
        {loadErr !== "" && (
          <div className="w-err" role="alert">
            {loadErr}
          </div>
        )}
      </main>

      <aside className="w-rail">
        <div className="w-tabs" role="tablist">
          {!share && (
            <TabButton id="explain" tab={tab} setTab={setTab} icon="stars">
              Explain
            </TabButton>
          )}
          <TabButton id="transcript" tab={tab} setTab={setTab} icon="chat-dots">
            Transcript
          </TabButton>
          <TabButton id="chapters" tab={tab} setTab={setTab} icon="list-ul">
            Chapters
          </TabButton>
        </div>
        {tab === "explain" && !share && (
          <Explain
            turns={tutor.turns}
            asking={tutor.asking}
            ready={script !== null}
            at={t}
            where={where}
            duration={duration}
            paused={paused && !playing}
            onAsk={ask}
            onSeek={seek}
            onResume={() => playIt(videoRef.current)}
            onClear={tutor.clear}
          />
        )}
        {tab === "transcript" && <Transcript lines={script?.lines ?? []} t={t} seek={seek} />}
        {tab === "chapters" && <Chapters chapters={chapters} scenes={scenes} t={t} seek={seek} />}
      </aside>
    </div>
  );
}

function TabButton({ id, tab, setTab, icon, children }: {
  id: Tab;
  tab: Tab;
  setTab: (t: Tab) => void;
  icon: string;
  children: string;
}) {
  return (
    <button className={tab === id ? "w-tab on" : "w-tab"} role="tab" aria-selected={tab === id}
      onClick={() => setTab(id)}>
      <Icon name={icon} />
      {children}
    </button>
  );
}
