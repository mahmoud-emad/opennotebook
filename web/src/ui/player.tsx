// The play page: a narrated deck or an audio overview, with a transcript and a
// question asked out loud. The old `player.html` as a lazy route of the app,
// with the same markup, ids, classes, words and look; `playerEngine.ts` holds
// its behaviour.

import { useEffect, useRef, useState, type CSSProperties } from "react";
import "../styles/player.css";
import { Icon } from "./Icon";
import { PlayerEngine } from "./playerEngine";
import { displayName, FORMATS, fitBox, initialsOf, metaOf, slideName, speakerName, tocLine, transcript, voiceIndex } from "./playerModel";
import { useStore } from "./store";

export function PlayerPage({ sid, share }: { sid: string; share: string | null }) {
  const [engine] = useState(() => new PlayerEngine({ sid, share }));
  const v = useStore(engine.view);
  const audioRef = useRef<HTMLAudioElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const [stage, setStage] = useState({ width: 0, height: 0 });
  const [fs, setFs] = useState(false);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    const detach = engine.attach(audio);
    void engine.boot();
    const title = document.title;
    document.body.classList.add("player-open");
    const onKey = (e: KeyboardEvent) => engine.onKey(e);
    window.addEventListener("keydown", onKey);
    engine.setFullscreen(() => {
      const st = stageRef.current;
      if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
      else if (st?.requestFullscreen) void st.requestFullscreen().catch(() => {});
    });
    const onFs = () => setFs(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", onFs);
    return () => {
      detach();
      engine.dispose();
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("fullscreenchange", onFs);
      document.body.classList.remove("player-open");
      document.title = title;
    };
  }, [engine]);

  // The slide's box follows the stage: on a resize, the panel opening or
  // closing, and full screen.
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      setStage((s) => (s.width === r.width && s.height === r.height ? s : { width: r.width, height: r.height }));
    };
    if (typeof ResizeObserver === "undefined") {
      const first = requestAnimationFrame(measure);
      window.addEventListener("resize", measure);
      return () => {
        cancelAnimationFrame(first);
        window.removeEventListener("resize", measure);
      };
    }
    // An observer reports the size it starts with, then every change.
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // The transcript follows the voice until the listener scrolls it.
  useEffect(() => {
    if (!v.reveal || !v.follow) return;
    document.getElementById(v.reveal.id)?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  }, [v.reveal, v.follow]);

  const s = v.session;
  const audioMode = !!s?.audio;
  const ready = s?.state === "ready" && v.flat.length > 0;
  const busy = v.turnBusy;
  const cur = v.idx >= 0 && v.idx < v.flat.length ? v.flat[v.idx]! : null;
  const curOrdinal = cur ? cur.slide.ordinal : -1;
  const box = fitBox((cur ?? v.flat[0])?.slide.aspect ?? null, stage);
  const frameStyle: CSSProperties = {
    width: `${box.width}px`,
    height: `${box.height}px`,
    transform: `scale(${box.scale})`,
  };
  const unit = audioMode ? "chapter" : "slide";
  const Unit = audioMode ? "Chapter" : "Slide";
  const meta = s ? metaOf(s, v.flat) : "";
  const mic = busy ? v.mic : "idle";
  const rows = transcript(v.flat, v.msgs, audioMode);

  return (
    <div className={audioMode ? "player-page audio" : "player-page"}>
      <div id="app" className={[v.rail ? "" : "no-rail", v.captions ? "" : "no-cap"].filter(Boolean).join(" ")}>
        <header id="top">
          {/* The same mark as the studio's own header, and the same way home.
              Here it asks first, because leaving drops the place. */}
          <button id="leave" className="brand" title="Back to the collection" onClick={() => engine.leave()}>
            <span className="mark">
              <Icon name="collection-play" />
            </span>
            <span>Studio</span>
          </button>
          <div id="titlebox">
            <div id="title">{s?.title ?? ""}</div>
            <div id="sub">{meta}</div>
          </div>
          <span id="onair" role="status" className={v.onair.live ? "live" : ""}>
            <span className="dot" />
            <span id="onair-t">{v.onair.label}</span>
          </span>
        </header>

        <main id="left">
          <div id="stage" ref={stageRef}>
            <div id="box" style={{ width: `${box.boxWidth}px`, height: `${box.boxHeight}px` }}>
              <iframe
                id="slide"
                sandbox="allow-same-origin"
                title="Slide"
                hidden={audioMode || v.shown.kind === "img"}
                srcDoc={v.shown.kind === "html" ? v.shown.html : undefined}
                style={frameStyle}
              />
              {/* An audio overview's stage, in place of the slide. */}
              <div id="astage" hidden={!audioMode} className={v.now.name && v.now.moving ? "moving" : ""}>
                <div id="as-fmt">{s?.audio ? `${FORMATS[s.audio.format] || "Audio"} · audio overview` : ""}</div>
                <h2 id="as-title">{s?.title ?? ""}</h2>
                <div id="as-chap">{v.chapter}</div>
                <div id="as-hosts">
                  {(s?.speakers ?? []).map((sp) => {
                    const name = displayName(sp);
                    return (
                      <div
                        key={sp.speaker_id}
                        className={v.now.name && v.now.name === name ? "as-host on" : "as-host"}
                        data-name={name}
                      >
                        <div className="as-av">{initialsOf(name)}</div>
                        <div className="as-nm">{name}</div>
                      </div>
                    );
                  })}
                </div>
                <div className="as-wave" aria-hidden="true">
                  {Array.from({ length: 20 }, (_, i) => (
                    <i key={i} />
                  ))}
                </div>
              </div>
              <img
                id="slideimg"
                alt="Slide"
                hidden={audioMode || v.shown.kind !== "img"}
                src={v.shown.kind === "img" ? v.shown.url : undefined}
              />
              {/* Inside the slide's own box, so they cover exactly the slide. */}
              <div id="poster" className="veil" hidden={!(ready && v.idx < 0 && !busy && !v.ended)}>
                <div className="veil-card">
                  <button
                    id="start"
                    title="Start (K)"
                    aria-label={audioMode ? "Play the audio overview" : "Start the session"}
                    disabled={!ready}
                    onClick={() => void engine.playNow()}
                  >
                    <Icon name="play-fill" />
                  </button>
                  <h2 id="poster-title">{s?.title ?? ""}</h2>
                  <p id="poster-meta">{meta}</p>
                  <p>
                    Ask a question out loud at any moment: press <span className="kbd">Space</span> or the Ask button.
                  </p>
                </div>
              </div>
              <div id="endcard" className="veil" hidden={!v.ended}>
                <div className="veil-card">
                  <h2>{audioMode ? "That's the episode" : "That's the session"}</h2>
                  <p>Ask a follow-up question below, watch it again, or go back to your studio.</p>
                  <div className="veil-row">
                    <button id="replay" className="primary" onClick={() => void engine.jumpTo(0, 0)}>
                      <Icon name="arrow-counterclockwise" /> {audioMode ? "Listen again" : "Watch again"}
                    </button>
                    <button id="home" onClick={() => engine.goHome()}>
                      <Icon name="grid" /> Back to collection
                    </button>
                  </div>
                </div>
              </div>
            </div>
            <div id="now" className={[v.now.name ? "show" : "", v.now.moving ? "" : "paused"].filter(Boolean).join(" ")}>
              <span className="eq">
                <i />
                <i />
                <i />
              </span>
              <span className="nm">{v.now.name}</span>
            </div>
            <div id="cap" hidden={v.caption === null} aria-live="off">
              {v.caption ?? ""}
            </div>

            <div id="prep" hidden={v.phase === "ready"}>
              <div id="prep-card">
                <div id="stage-name">{v.stageName}</div>
                <div id="bar" className={v.barIdle ? "idle" : ""}>
                  <div id="fill" style={v.fill ? { width: v.fill } : undefined} />
                </div>
                <div id="prep-sub">{v.prepSub}</div>
              </div>
            </div>
          </div>

          <div id="controls">
            <div
              id="timeline"
              className={!ready || busy ? "off" : ""}
              role="slider"
              aria-label="Session progress"
              tabIndex={-1}
              onClick={(e) => {
                const r = e.currentTarget.getBoundingClientRect();
                if (r.width > 0) engine.seekFraction((e.clientX - r.left) / r.width);
              }}
            >
              {v.parts.map((p, k) => (
                <div
                  key={p.ordinal}
                  className={p.ordinal === curOrdinal ? "seg on" : "seg"}
                  data-o={p.ordinal}
                  title={`${p.ordinal + 1}. ${slideName(p, audioMode)}`}
                  style={{
                    flexGrow: Math.max(
                      v.flat.filter((f) => f.slide.ordinal === p.ordinal).reduce((a, f) => a + (f.line.duration_ms || 0), 0),
                      1,
                    ),
                  }}
                >
                  <div className="f" style={{ width: `${(v.fills[k] ?? 0).toFixed(2)}%` }} />
                </div>
              ))}
            </div>
            <div id="ctl-row">
              <button
                id="prev"
                className="icon"
                disabled={!ready || busy}
                title={`Previous ${unit} (←)`}
                aria-label={`Previous ${unit}`}
                onClick={() => engine.stepSlide(-1)}
              >
                <Icon name="skip-start-fill" />
              </button>
              <button
                id="playpause"
                className="icon"
                disabled={!ready || busy}
                title={v.playing ? "Pause (K)" : "Play (K)"}
                aria-label={v.playing ? "Pause" : "Play"}
                onClick={() => engine.playPause()}
              >
                <Icon name={v.playing ? "pause-fill" : "play-fill"} />
              </button>
              <button
                id="next"
                className="icon"
                disabled={!ready || busy}
                title={`Next ${unit} (→)`}
                aria-label={`Next ${unit}`}
                onClick={() => engine.stepSlide(1)}
              >
                <Icon name="skip-end-fill" />
              </button>
              <span id="clock">{v.clock}</span>
              <span id="slidepos">
                {cur && (
                  <>
                    <b>{`${Unit} ${curOrdinal + 1} of ${v.parts.length}`}</b>
                    {` · ${slideName(cur.slide, audioMode)}`}
                  </>
                )}
              </span>
              <span id="state" data-s={v.state}>
                {v.state}
              </span>
              <button
                id="speed"
                title="Playback speed"
                aria-label={`Playback speed, ${v.speed} times`}
                onClick={() => engine.cycleSpeed()}
              >
                {`${v.speed}×`}
              </button>
              <a
                id="dl"
                className="icon"
                hidden={!audioMode}
                href={v.episode || undefined}
                download
                title="Download the episode (WAV)"
                aria-label="Download the episode"
              >
                <Icon name="download" />
              </a>
              <button
                id="cc"
                className={v.captions ? "icon on" : "icon"}
                title="Captions (C)"
                aria-label="Captions"
                aria-pressed={v.captions}
                onClick={() => engine.toggleCaptions()}
              >
                <Icon name="badge-cc" />
              </button>
              <button
                id="railbtn"
                className={v.rail ? "icon on" : "icon"}
                title="Transcript panel"
                aria-label="Transcript panel"
                aria-pressed={v.rail}
                onClick={() => engine.toggleRail()}
              >
                <Icon name="layout-sidebar-reverse" />
              </button>
              <button
                id="fs"
                className="icon"
                title="Full screen (F)"
                aria-label="Full screen"
                onClick={() => engine.fullscreen()}
              >
                <Icon name={fs ? "fullscreen-exit" : "fullscreen"} />
              </button>
            </div>
          </div>
        </main>

        <aside id="rail">
          <div id="tabs" role="tablist">
            <button
              className={v.tab === "lines" ? "tab on" : "tab"}
              role="tab"
              data-pane="lines"
              aria-selected={v.tab === "lines"}
              onClick={() => engine.setTab("lines")}
            >
              Transcript
            </button>
            <button
              className={v.tab === "toc" ? "tab on" : "tab"}
              role="tab"
              data-pane="toc"
              aria-selected={v.tab === "toc"}
              onClick={() => engine.setTab("toc")}
            >
              {audioMode ? "Chapters" : "Slides"}
            </button>
          </div>
          <div id="panes">
            <div
              id="lines"
              role="log"
              aria-label="Transcript and questions"
              hidden={v.tab !== "lines"}
              onWheel={() => engine.scrolled()}
              onTouchMove={() => engine.scrolled()}
            >
              {s &&
                rows.map((r) => {
                  if (r.kind === "chap")
                    return (
                      <div key={r.key} className="chap">
                        {r.text}
                      </div>
                    );
                  if (r.kind === "msg") {
                    const m = r.msg;
                    return (
                      <div key={r.key} id={m.id} className={m.cls ? `msg ${m.cls}` : "msg"} title={m.title}>
                        <div className="bub">
                          <div className="nm">{m.who}</div>
                          <div className="tx">
                            {m.text}
                            {m.live && <span className="caret"> </span>}
                          </div>
                        </div>
                      </div>
                    );
                  }
                  const f = v.flat[r.i]!;
                  const cls = [
                    "msg",
                    "line",
                    `v${voiceIndex(s, f.line.speaker_id)}`,
                    r.i < v.seenBefore ? "seen" : "",
                    r.i === v.onLine ? "on" : "",
                  ]
                    .filter(Boolean)
                    .join(" ");
                  return (
                    <div
                      key={r.key}
                      id={`L${r.i}`}
                      className={cls}
                      tabIndex={0}
                      title="Play from here"
                      onClick={() => void engine.jumpTo(r.i, 0)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") void engine.jumpTo(r.i, 0);
                      }}
                    >
                      <div className="bub">
                        <div className="nm">{speakerName(s, f.line.speaker_id)}</div>
                        <div className="tx">{f.line.text}</div>
                      </div>
                    </div>
                  );
                })}
            </div>
            <div id="toc" hidden={v.tab !== "toc"}>
              {s &&
                v.parts.map((p) => (
                  <button
                    key={p.ordinal}
                    className={p.ordinal === curOrdinal ? "toc-item on" : "toc-item"}
                    data-o={p.ordinal}
                    onClick={() => engine.jumpToPart(p.ordinal)}
                  >
                    <span className="toc-n">{p.ordinal + 1}</span>
                    <span className="toc-t">
                      <b>{slideName(p, audioMode)}</b>
                      <span>{tocLine(s, v.flat, p.ordinal)}</span>
                    </span>
                  </button>
                ))}
            </div>
            <button id="follow" hidden={v.follow || v.tab !== "lines"} onClick={() => engine.backToNow()}>
              <Icon name="arrow-down" /> Back to now
            </button>
          </div>
          <div id="err" hidden={!v.err}>
            {v.err}
          </div>
          <div id="mic">
            <div id="composer">
              <button
                id="ask"
                hidden={mic !== "idle"}
                disabled={!ready || busy}
                title="Ask a question (Space)"
                onClick={() => void engine.askStart()}
              >
                <Icon name="mic-fill" />
                <span>Ask a question</span>
                <span className="kbd">Space</span>
              </button>
              <Meter engine={engine} hidden={mic !== "listening"} />
              <div id="ctext" hidden={mic !== "listening"}>
                {mic === "listening" ? v.ctext : ""}
              </div>
              <button
                id="discard"
                className="icon"
                hidden={mic !== "listening"}
                title="Discard"
                aria-label="Discard"
                onClick={() => void engine.askCancel()}
              >
                <Icon name="x-lg" />
              </button>
              <button
                id="send"
                className="icon send"
                hidden={mic !== "listening"}
                title="Send"
                aria-label="Send"
                onClick={() => void engine.askSend()}
              >
                <Icon name="send-fill" />
              </button>
            </div>
            <div id="mic-state">{v.micState}</div>
          </div>
        </aside>
      </div>

      <div id="confirm" className={v.confirm !== null ? "show" : ""} role="dialog" aria-modal="true" aria-labelledby="confirm-h">
        <div id="confirm-card">
          <h3 id="confirm-h">Leave this session?</h3>
          <p id="confirm-why">{v.confirm ?? ""}</p>
          <div id="confirm-row">
            <button id="stay" onClick={() => engine.stay()}>
              Stay
            </button>
            <button id="do-leave" onClick={() => void engine.doLeave()}>
              Leave
            </button>
          </div>
        </div>
      </div>

      <audio id="audio" ref={audioRef} preload="auto" />
    </div>
  );
}

/** The level meter: five bars that move only while someone is talking. */
function Meter({ engine, hidden }: { engine: PlayerEngine; hidden: boolean }) {
  const bars = useStore(engine.meter);
  return (
    <span id="meter" hidden={hidden}>
      {bars.map((h, k) => (
        <i key={k} style={{ height: `${h}px` }} />
      ))}
    </span>
  );
}
