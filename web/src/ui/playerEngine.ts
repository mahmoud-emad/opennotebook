// The player's behaviour: playback, the playhead, the slide on the stage, the
// transcript, and the voice turn (stop, listen, answer, resume). A port of the
// script in the old `player.html`, function for function; the page draws what
// this holds (`view`) and hands it the clicks and keys.

import { errText, refusal } from "./api";
import { UNREACHABLE } from "./errors";
import {
  doneMs,
  extendMs,
  fills,
  flatten,
  hhmmss,
  keyAction,
  languageTag,
  nextSpeed,
  partsOf,
  say,
  seekTarget,
  slideKind,
  slideName,
  soundsUnfinished,
  speakerName,
  SPEEDS,
  stepTarget,
  toWav,
  totalMs,
  b64ToPcm16,
  captionAt,
  captionParts,
  type Flat,
  type Msg,
  type Part,
  type SessionDoc,
} from "./playerModel";
import {
  episodeUrl,
  eventsUrl,
  headsFor,
  lineAudioUrl,
  loadSession,
  slideUrl,
  voiceUrl,
  type Heads,
  type Playhead,
  type Source,
} from "./playerApi";
import { sseFrames } from "./sse";
import { Mic, makeSink, startRecognition, type Sink } from "./playerVoice";
import { go, type View } from "./routes";
import { SETTINGS, settingValue } from "./settings";
import { store, type Store } from "./store";
import { storage } from "./api";

/** What the stage shows: a slide's own document, a picture, or nothing yet. */
export type Shown = { kind: "html"; html: string } | { kind: "img"; url: string } | { kind: "none" };

export type MicMode = "idle" | "listening" | "working";

/** Everything the page draws. */
export type PlayerView = {
  phase: "loading" | "preparing" | "ready" | "failed" | "missing";
  /** The card over the stage while there is nothing to play. */
  stageName: string;
  prepSub: string;
  barIdle: boolean;
  fill: string;
  /** The session's state, kept on the page for anyone looking. */
  state: string;
  session: SessionDoc | null;
  flat: Flat[];
  parts: Part[];
  idx: number;
  /** The line lit in the transcript, and the lines before it, heard. */
  onLine: number;
  seenBefore: number;
  playing: boolean;
  ended: boolean;
  turnBusy: boolean;
  onair: { live: boolean; label: string };
  now: { name: string; moving: boolean };
  caption: string | null;
  shown: Shown;
  chapter: string;
  clock: string;
  fills: number[];
  speed: number;
  captions: boolean;
  rail: boolean;
  tab: "lines" | "toc";
  follow: boolean;
  msgs: Msg[];
  err: string;
  mic: MicMode;
  micState: string;
  ctext: string;
  /** Why leaving loses the place, while the leave question is open. */
  confirm: string | null;
  episode: string;
  /** An element to bring into view, once per change of `n`. */
  reveal: { id: string; n: number } | null;
};

const SPEED_KEY = "opennotebook_speed";
const CC_KEY = "opennotebook_cc";
const INTERRUPT_KEY = "OPENNOTEBOOK_INTERRUPT";
const PATIENCE_KEY = "OPENNOTEBOOK_TURN_PATIENCE";
const LANGUAGE_KEY = "OPENNOTEBOOK_LANGUAGE";
/** Input level that counts as the listener, after echo cancellation. */
const BARGE_PEAK = 0.08;
/** Meter polls (60 ms each) of it, about 300 ms, before cutting in. */
const BARGE_POLLS = 5;
/** Audio kept from before the detector fired. */
const PREFIX_MS = 300;
const POLL_MS = 3000;
/** How often the place is kept while playing, at most. A pause, a seek, the
 * end and leaving keep it at once besides. */
const REPORT_MS = 5000;

function stored(key: string): string | null {
  try {
    return storage()?.getItem(key) ?? null;
  } catch {
    return null;
  }
}
function keep(key: string, value: string): void {
  try {
    storage()?.setItem(key, value);
  } catch {
    // Kept for this page only.
  }
}

function initialView(): PlayerView {
  const speed = Number(stored(SPEED_KEY)) || 1;
  return {
    phase: "loading",
    stageName: "Getting ready…",
    prepSub: "",
    barIdle: true,
    fill: "",
    state: "",
    session: null,
    flat: [],
    parts: [],
    idx: -1,
    onLine: -1,
    seenBefore: -1,
    playing: false,
    ended: false,
    turnBusy: false,
    onair: { live: false, label: "OFF AIR" },
    now: { name: "", moving: false },
    caption: null,
    shown: { kind: "none" },
    chapter: "",
    clock: "00:00 / 00:00",
    fills: [],
    speed: SPEEDS.includes(speed) ? speed : 1,
    captions: stored(CC_KEY) !== "0",
    rail: true,
    tab: "lines",
    follow: true,
    msgs: [],
    err: "",
    mic: "idle",
    micState: "",
    ctext: "",
    confirm: null,
    episode: "",
    reveal: null,
  };
}

const escapeHtml = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

export class PlayerEngine {
  readonly view: Store<PlayerView>;
  /** The level meter's five bars, apart from the view: it moves 16 times a
   * second and nothing else on the page needs to. */
  readonly meter = store<number[]>([3, 3, 3, 3, 3]);

  private v: PlayerView;
  /** Full screen, which needs the stage element: set by the page. */
  private fs: (() => void) | null = null;
  private readonly src: Source;
  private readonly heads: Heads;
  private audio: HTMLAudioElement | null = null;
  private disposed = false;
  /** Which attach this is. React mounts the page twice in development, and a
   * boot from the first mount that answers after the second has started must
   * not start a second stream and poll beside the second boot's. */
  private life = 0;
  private playToken = 0;
  private slideToken = 0;
  private shownOrdinal = -1;
  private lastReport = 0;
  private pausedOffset = 0;
  private capLine = -1;
  private cap = { parts: [] as string[], ends: [] as number[] };
  private es: EventSource | null = null;
  private poll: ReturnType<typeof setTimeout> | null = null;
  private seq = 0;
  private revealN = 0;

  // The voice turn.
  private mic: Mic;
  private armed = false;
  private loudPolls = 0;
  private onBarge: (() => void) | null = null;
  private readonly voices = new Set<{ sink: Sink; ctrl: AbortController }>();
  private speech: { stop: () => void } | null = null;
  private heardText = "";
  private liveMsg: string | null = null;
  private answerMsg: string | null = null;
  private resumeAfter: { i: number; ended: boolean } | null = null;
  private cancelled = false;
  private sending = false;
  private extendTimer: ReturnType<typeof setTimeout> | null = null;
  private vadWarned = false;
  private bargeOn = true;
  private extend = 2000;
  private lang = "en-US";

  constructor(src: Source) {
    this.src = src;
    this.heads = headsFor(src);
    this.v = initialView();
    this.view = store(this.v);
    this.mic = new Mic({
      onEvent: (ev) => this.micEvent(ev),
      onEnd: () => this.micEnd(),
      onDegraded: (why) => this.vadDegraded(why),
      onLevel: (peak) => this.level(peak),
    });
  }

  private emit(): void {
    if (this.disposed) return;
    this.v = { ...this.v };
    this.view.set(this.v);
  }

  private cur(): Flat | null {
    const { idx, flat } = this.v;
    return idx >= 0 && idx < flat.length ? flat[idx]! : null;
  }

  /** Whether the session can be played: ready, with lines. */
  get ready(): boolean {
    return this.v.session?.state === "ready" && this.v.flat.length > 0;
  }

  // ── wiring ─────────────────────────────────────────────────────────────────

  setFullscreen(fn: () => void): void {
    this.fs = fn;
  }

  fullscreen(): void {
    this.fs?.();
  }

  attach(audio: HTMLAudioElement): () => void {
    // Attached again after a dispose when React mounts the page twice.
    this.disposed = false;
    this.life++;
    this.audio = audio;
    const onError = () => {
      const e = audio.error;
      // MEDIA_ERR_ABORTED is the fetch cancelled because the source changed,
      // which is what moving to the next line does.
      if (this.disposed || !e || e.code === 1 || !audio.getAttribute("src")) return;
      console.warn(`opennotebook: audio failed (code ${e.code}) for`, audio.src);
      this.v.err = say.cannotPlay;
      this.v.playing = false;
      this.emit();
    };
    const onEnded = () => {
      if (!this.v.playing) return;
      if (this.v.idx + 1 < this.v.flat.length) void this.startAt(this.v.idx + 1, 0);
      else void this.finish();
    };
    const onPlay = () => {
      if (audio.playbackRate !== this.v.speed) audio.playbackRate = this.v.speed;
    };
    const onTime = () => this.timeupdate();
    audio.addEventListener("error", onError);
    audio.addEventListener("ended", onEnded);
    audio.addEventListener("play", onPlay);
    audio.addEventListener("timeupdate", onTime);
    this.applySpeed();
    return () => {
      audio.removeEventListener("error", onError);
      audio.removeEventListener("ended", onEnded);
      audio.removeEventListener("play", onPlay);
      audio.removeEventListener("timeupdate", onTime);
    };
  }

  dispose(): void {
    // Left while playing, by Back or a link: the place is kept as it was,
    // read before the audio lets go of it.
    if (!this.disposed && this.v.playing && this.v.idx >= 0) void this.keepPlace("paused");
    this.disposed = true;
    if (this.audio) {
      this.audio.pause();
      this.audio.removeAttribute("src");
    }
    this.es?.close();
    this.es = null;
    if (this.poll) clearTimeout(this.poll);
    if (this.extendTimer) clearTimeout(this.extendTimer);
    this.hush();
    this.stopSpeech();
    this.mic.teardown();
  }

  // ── the session ────────────────────────────────────────────────────────────

  async boot(): Promise<void> {
    const life = this.life;
    let s: SessionDoc;
    try {
      s = await loadSession(this.src);
    } catch (e) {
      if (this.disposed || life !== this.life) return;
      this.v.phase = "missing";
      this.v.state = "";
      this.v.stageName = errText(e);
      this.v.prepSub = "";
      this.v.barIdle = false;
      this.emit();
      return;
    }
    if (this.disposed || life !== this.life) return;
    this.setSession(s);
    this.v.state = s.state;
    if (s.state === "ready") this.becameReady();
    else if (s.state === "preparing") {
      this.v.phase = "preparing";
      this.v.stageName = "preparing this session";
      this.emit();
      this.follow();
    } else this.failed();
  }

  private setSession(s: SessionDoc): void {
    this.v.session = s;
    this.v.flat = flatten(s);
    this.v.parts = partsOf(s);
    this.v.clock = `00:00 / ${hhmmss(totalMs(this.v.flat))}`;
    this.v.fills = this.v.parts.map(() => 0);
    this.v.episode = s.audio ? episodeUrl(this.src) : "";
    document.title = s.title ? `${s.title} · Studio` : "Studio";
  }

  private becameReady(): void {
    this.v.phase = "ready";
    this.v.state = "ready";
    this.emit();
    const first = this.v.flat[0];
    if (first) void this.showSlide(first.slide);
  }

  private failed(): void {
    this.v.phase = "failed";
    this.v.state = "failed";
    this.v.stageName = "This could not be made";
    this.v.prepSub = "Go back to the studio to see why, then try again.";
    // A sweeping bar on a dead job is the page claiming work is happening.
    this.v.barIdle = false;
    this.v.fill = "0%";
    this.v.onair = { live: false, label: "FAILED" };
    this.emit();
  }

  /** While it is made: the build's progress where the studio streams it, and
   * the output itself, read again every few seconds until it is ready. */
  private follow(): void {
    const url = eventsUrl(this.src);
    if (url && typeof EventSource !== "undefined") {
      const es = new EventSource(url);
      this.es = es;
      const onProg = (e: MessageEvent<string>) => {
        try {
          const p = JSON.parse(e.data) as { label?: string; steps_done?: number; steps_total?: number };
          const known = (p.steps_total ?? 0) > 0;
          // Said in the card's lower case, as its other lines are.
          const label = p.label || "Starting";
          this.v.stageName = label[0]!.toLowerCase() + label.slice(1);
          this.v.barIdle = !known;
          this.v.prepSub = known ? `step ${p.steps_done} of ${p.steps_total}` : "";
          this.v.fill = known ? `${(100 * (p.steps_done ?? 0)) / (p.steps_total ?? 1)}%` : "";
          this.emit();
        } catch {
          // A frame that is not progress.
        }
      };
      es.addEventListener("prep.progress", onProg as EventListener);
      es.addEventListener("progress", onProg as EventListener);
      // The reading below sees every change of state; a stream that cannot
      // be opened is not retried.
      es.onerror = () => es.close();
    }
    const life = this.life;
    const gone = () => this.disposed || life !== this.life;
    const tick = async () => {
      if (gone()) return;
      try {
        const s = await loadSession(this.src);
        if (gone()) return;
        if (s.state !== "preparing") {
          this.es?.close();
          this.es = null;
          this.setSession(s);
          if (s.state === "ready") this.becameReady();
          else this.failed();
          return;
        }
      } catch {
        // Asked again on the next tick.
      }
      this.poll = setTimeout(() => void tick(), POLL_MS);
    };
    this.poll = setTimeout(() => void tick(), POLL_MS);
  }

  // ── the stage ──────────────────────────────────────────────────────────────

  private async showSlide(s: Part): Promise<void> {
    if (this.v.session?.audio) {
      this.v.chapter = `Chapter ${s.ordinal + 1} of ${this.v.parts.length} · ${slideName(s, true)}`;
      this.emit();
      return;
    }
    if (s.ordinal === this.shownOrdinal) return;
    this.shownOrdinal = s.ordinal;
    const token = ++this.slideToken;
    const url = slideUrl(this.src, s.ordinal);
    let shown: Shown;
    try {
      const r = await fetch(url);
      if (!r.ok) {
        const why = await refusal(r);
        shown = {
          kind: "html",
          html: `<p style="color:#f85149;font:40px/1.4 system-ui,sans-serif;margin:60px">${escapeHtml(why)}</p>`,
        };
      } else if (slideKind(r.headers.get("content-type")) === "img") {
        shown = { kind: "img", url };
      } else {
        // srcdoc, not src: the slide is self-contained with no base URL.
        shown = { kind: "html", html: await r.text() };
      }
    } catch {
      shown = {
        kind: "html",
        html: `<p style="color:#f85149;font:40px/1.4 system-ui,sans-serif;margin:60px">${escapeHtml(UNREACHABLE)}</p>`,
      };
      // Asked again the next time this slide comes up.
      this.shownOrdinal = -1;
    }
    if (token !== this.slideToken || this.disposed) return;
    this.v.shown = shown;
    this.emit();
  }

  // ── playback ───────────────────────────────────────────────────────────────

  private at(state: Playhead["state"]): Playhead {
    const c = this.cur();
    return {
      slide_ordinal: c ? c.slide.ordinal : 0,
      line_id: c ? c.line.line_id : "",
      // The audio element is the only clock that knows what was heard, and
      // the exact millisecond is kept, never rounded to a line.
      offset_ms: Math.round((this.audio?.currentTime || 0) * 1000),
      state,
    };
  }

  private reveal(id: string): void {
    this.v.reveal = { id, n: ++this.revealN };
  }

  private async startAt(i: number, offsetMs: number): Promise<void> {
    const audio = this.audio;
    if (!audio) return;
    this.v.idx = i;
    this.v.ended = false;
    const c = this.cur();
    if (!c) return this.finish();
    await this.showSlide(c.slide);
    if (this.disposed) return;
    // Everything before the playhead is heard: the same boundary an answer
    // is grounded in.
    this.v.onLine = i;
    this.v.seenBefore = i;
    this.reveal(`L${i}`);
    audio.src = lineAudioUrl(this.src, c.line.line_id);
    audio.currentTime = (offsetMs || 0) / 1000;
    this.v.playing = true;
    this.markPosition();
    this.nowSpeaking(speakerName(this.v.session, c.line.speaker_id), true);
    this.onAir(true, "LIVE");
    this.emit();
    // A rejected play() is the one failure a listener cannot see. But an
    // AbortError is a newer source or a pause landing first, which happens on
    // every line change: whoever superseded it owns what happens next.
    const token = ++this.playToken;
    try {
      await audio.play();
      this.v.err = "";
    } catch (e) {
      const name = e instanceof Error ? e.name : "";
      if (name === "AbortError") return;
      if (name === "NotAllowedError") this.v.err = say.blocked;
      else {
        console.warn(`opennotebook: cannot play ${c.line.line_id}:`, e);
        this.v.err = say.cannotPlay;
      }
      this.v.playing = false;
      this.emit();
      return;
    }
    if (token !== this.playToken) return;
    this.emit();
    // Told, not awaited: blocking the next line on a round trip is a stutter.
    void this.keepPlace("playing");
  }

  private async finish(): Promise<void> {
    this.v.playing = false;
    this.v.ended = true;
    this.audio?.pause();
    this.onAir(false, "ENDED");
    this.nowSpeaking("", false);
    this.v.caption = null;
    this.emit();
    await this.keepPlace("finished");
  }

  private timeupdate(): void {
    const { flat, idx } = this.v;
    const secs = this.audio?.currentTime || 0;
    if (flat.length) {
      this.v.clock = `${hhmmss(doneMs(flat, idx, secs, false))} / ${hhmmss(totalMs(flat))}`;
      if (totalMs(flat)) this.v.fills = fills(flat, this.v.parts, doneMs(flat, idx, secs));
    }
    this.caption();
    this.emit();
    if (this.v.playing && Date.now() - this.lastReport >= REPORT_MS) void this.keepPlace("playing");
  }

  /** Keep the place where it is kept (the server, or this browser for a
   * share's), told rather than awaited. */
  private keepPlace(state: Playhead["state"]): Promise<void> {
    this.lastReport = Date.now();
    return this.heads.put(this.at(state)).catch(() => {});
  }

  private markPosition(): void {
    this.capLine = -1;
    this.caption();
  }

  /** The sentence being said, not the whole line: a paragraph over the
   * slide hides the slide. */
  private caption(): void {
    const c = this.cur();
    if (!c || !this.v.playing) {
      this.v.caption = null;
      return;
    }
    if (this.capLine !== this.v.idx) {
      this.capLine = this.v.idx;
      this.cap = captionParts(c.line.text);
    }
    this.v.caption = captionAt(this.cap, this.audio?.currentTime || 0, c.line.duration_ms || 0);
  }

  private onAir(live: boolean, label: string): void {
    this.v.onair = { live, label: label || (live ? "LIVE" : "OFF AIR") };
  }

  private nowSpeaking(name: string, moving: boolean): void {
    this.v.now = { name, moving };
  }

  async jumpTo(i: number, offsetMs: number): Promise<void> {
    if (this.v.turnBusy || !this.v.flat.length || i < 0 || i >= this.v.flat.length) return;
    this.audio?.pause();
    this.v.follow = true;
    await this.startAt(i, offsetMs || 0);
  }

  /** A click on the scrubber, as a fraction of its width. */
  seekFraction(frac: number): void {
    if (!this.ready || this.v.turnBusy) return;
    const t = seekTarget(this.v.flat, totalMs(this.v.flat) * Math.min(1, Math.max(0, frac)));
    if (t) void this.jumpTo(t[0], t[1]);
  }

  stepSlide(dir: -1 | 1): void {
    const i = stepTarget(this.v.flat, this.v.parts, this.v.idx, dir);
    if (i >= 0) void this.jumpTo(i, 0);
  }

  jumpToPart(ordinal: number): void {
    const i = this.v.flat.findIndex((f) => f.slide.ordinal === ordinal);
    if (i >= 0) void this.jumpTo(i, 0);
  }

  async playNow(): Promise<void> {
    if (this.v.ended) return this.jumpTo(0, 0);
    if (this.v.idx < 0) return this.startAt(0, 0);
    // Resume from where the pause was recorded: a millisecond inside the
    // line, not its start.
    const c = this.cur();
    const h = await this.heads.get().catch(() => null);
    const off = h && c && h.line_id === c.line.line_id ? h.offset_ms : this.pausedOffset;
    await this.startAt(this.v.idx, off);
  }

  async pauseNow(): Promise<void> {
    this.v.playing = false;
    this.audio?.pause();
    this.onAir(false, "PAUSED");
    const c = this.cur();
    this.nowSpeaking(c ? speakerName(this.v.session, c.line.speaker_id) : "", false);
    this.v.caption = null;
    this.emit();
    const h = this.at("paused");
    this.pausedOffset = h.offset_ms;
    await this.heads.put(h).catch(() => {});
  }

  playPause(): void {
    void (this.v.playing ? this.pauseNow() : this.playNow());
  }

  private applySpeed(): void {
    const a = this.audio;
    if (a) {
      // A new source resets the rate to the default one, so both are set.
      a.defaultPlaybackRate = this.v.speed;
      a.playbackRate = this.v.speed;
    }
  }

  cycleSpeed(): void {
    this.v.speed = nextSpeed(this.v.speed);
    keep(SPEED_KEY, String(this.v.speed));
    this.applySpeed();
    this.emit();
  }

  toggleCaptions(): void {
    this.v.captions = !this.v.captions;
    keep(CC_KEY, this.v.captions ? "1" : "0");
    this.emit();
  }

  toggleRail(): void {
    this.v.rail = !this.v.rail;
    this.emit();
  }

  setTab(tab: "lines" | "toc"): void {
    this.v.tab = tab;
    this.emit();
  }

  /** The listener scrolled the transcript: it stops following the voice. */
  scrolled(): void {
    if (!this.v.follow) return;
    this.v.follow = false;
    this.emit();
  }

  backToNow(): void {
    this.v.follow = true;
    this.reveal(`L${Math.max(this.v.idx, 0)}`);
    this.emit();
  }

  // ── leaving ────────────────────────────────────────────────────────────────

  /** Where the way back leads: the shared collection for a share, else the
   * collection the output was made in. */
  homeView(): View {
    if (this.src.share) return { kind: "shared", id: this.src.share };
    const cid = this.v.session?.collection;
    return cid ? { kind: "collection", cid, open: null } : { kind: "discover" };
  }

  leave(): void {
    const c = this.cur();
    // Only asked when there is a place to lose.
    if (!c || this.v.idx < 0 || this.v.ended) {
      void this.doLeave();
      return;
    }
    this.v.confirm = say.leaveWhy(c.slide.ordinal, this.v.parts.length);
    this.emit();
  }

  stay(): void {
    this.v.confirm = null;
    this.emit();
  }

  async doLeave(): Promise<void> {
    this.v.playing = false;
    this.audio?.pause();
    this.es?.close();
    // Told rather than walked away from: nothing else would tell it.
    if (this.v.idx >= 0) await this.keepPlace("paused");
    go(this.homeView());
  }

  goHome(): void {
    go(this.homeView());
  }

  // ── keys ───────────────────────────────────────────────────────────────────

  onKey(e: KeyboardEvent): void {
    const target = e.target as HTMLElement | null;
    const a = keyAction(e, (target && target.tagName) || "", this.v.confirm !== null);
    const ready = this.ready && !this.v.turnBusy;
    switch (a) {
      case "stay":
        this.stay();
        break;
      case "ask":
        e.preventDefault();
        if (!this.v.turnBusy) {
          if (ready || this.src.share) void this.askStart();
        } else if (this.mic.open) void this.askCancel();
        break;
      case "playpause":
        e.preventDefault();
        if (ready) this.playPause();
        break;
      case "prev":
      case "next":
        if (ready) {
          e.preventDefault();
          this.stepSlide(a === "prev" ? -1 : 1);
        }
        break;
      case "captions":
        this.toggleCaptions();
        break;
      case "fullscreen":
        this.fullscreen();
        break;
    }
  }

  // ── the transcript's messages ──────────────────────────────────────────────

  /** A message where it happened: after the line that was playing. */
  private appendMsg(m: { who: string; text: string; cls: string }): string {
    const id = `m${++this.seq}`;
    this.v.msgs = [...this.v.msgs, { id, after: this.v.idx, who: m.who, text: m.text, cls: m.cls, live: false }];
    this.reveal(id);
    this.emit();
    return id;
  }

  private patchMsg(id: string, fn: (m: Msg) => Msg): void {
    this.v.msgs = this.v.msgs.map((m) => (m.id === id ? fn(m) : m));
  }

  private setText(id: string, text: string, live: boolean): void {
    this.patchMsg(id, (m) => ({ ...m, text, live }));
    this.reveal(id);
    this.emit();
  }

  private removeMsg(id: string | null): void {
    if (!id) return;
    this.v.msgs = this.v.msgs.filter((m) => m.id !== id);
  }

  private settle(id: string | null): void {
    if (id) this.patchMsg(id, (m) => ({ ...m, cls: m.cls.replace(/\bon\b/, "").trim() }));
  }

  // ── the voice turn ─────────────────────────────────────────────────────────

  private micEvent(ev: number): void {
    if (ev >= 1 && this.armed) {
      this.v.micState = "listening";
      // They carried on after a pause that sounded unfinished.
      if (this.extendTimer) clearTimeout(this.extendTimer);
      this.extendTimer = null;
      this.emit();
    }
  }

  private micEnd(): void {
    if (soundsUnfinished(this.heardText) && !this.extendTimer) {
      // Re-open the turn and give them longer; new speech clears the timer.
      this.mic.reset();
      this.v.micState = "listening… take your time";
      this.emit();
      this.extendTimer = setTimeout(() => {
        this.extendTimer = null;
        void this.askSend();
      }, this.extend);
    } else void this.askSend();
  }

  private level(peak: number): void {
    this.loudPolls = peak > BARGE_PEAK ? this.loudPolls + 1 : Math.max(0, this.loudPolls - 1);
    // The detector and the level both have to say it is a person, for a
    // moment, before the studio stops talking.
    if (this.bargeOn && this.onBarge && this.mic.speaking && this.loudPolls >= BARGE_POLLS) {
      const f = this.onBarge;
      this.onBarge = null;
      f();
    }
    this.meter.set([0, 1, 2, 3, 4].map((k) => Math.max(3, Math.min(1, peak * (1.6 - k * 0.15)) * 18)));
  }

  /** Said on screen, once a page, not only in a console nobody has open. */
  private vadDegraded(why: string): void {
    console.warn("opennotebook: the voice detector is unavailable, listening for loudness instead:", why);
    if (this.vadWarned) return;
    this.vadWarned = true;
    this.appendMsg({ who: "Studio", cls: "err", text: say.degraded });
  }

  /** Silence everything the studio is saying and cancel what it was about to. */
  private hush(): void {
    for (const v of this.voices) {
      try {
        v.ctrl.abort();
      } catch {
        // Already over.
      }
      v.sink.stop();
    }
    this.voices.clear();
  }

  private stopSpeech(): void {
    try {
      this.speech?.stop();
    } catch {
      // Already stopped.
    }
    this.speech = null;
  }

  private startSpeech(): void {
    this.heardText = "";
    this.speech = startRecognition(this.lang, (t) => {
      this.heardText = t;
      if (this.v.mic === "listening") {
        this.v.ctext = t;
        this.emit();
      }
    });
  }

  private micButtons(mode: MicMode): void {
    this.v.mic = mode;
    if (mode !== "listening") this.v.ctext = "";
  }

  async askStart(): Promise<void> {
    if (this.v.turnBusy) return;
    if (this.src.share) {
      this.v.micState = say.sharedAsk;
      this.emit();
      return;
    }
    if (!this.ready) return;
    const doc = SETTINGS.get().doc;
    this.bargeOn = settingValue(doc, INTERRUPT_KEY) !== "off";
    this.extend = extendMs(settingValue(doc, PATIENCE_KEY));
    this.lang = languageTag(settingValue(doc, LANGUAGE_KEY));
    this.v.turnBusy = true;
    this.cancelled = false;
    const c = this.cur();
    this.resumeAfter = { i: this.v.idx, ended: this.v.ended || !c };
    if (this.v.playing) {
      this.v.playing = false;
      this.audio?.pause();
      this.v.caption = null;
      this.emit();
      await this.keepPlace("paused");
    }
    // The first question loads the detector, which takes a moment.
    this.micButtons("working");
    this.v.micState = "Getting the microphone ready…";
    this.emit();
    try {
      await this.mic.start();
    } catch (e) {
      this.v.turnBusy = false;
      this.micButtons("idle");
      console.warn("opennotebook: microphone:", e);
      this.v.micState = say.mic(e instanceof Error ? e.name : undefined);
      this.emit();
      return;
    }
    // The question starts here: nothing the studio said before is in it.
    this.mic.captureFrom = this.mic.captured;
    this.mic.reset();
    if (this.cancelled) return;
    this.listen();
  }

  /** The floor is theirs, and the page says so. */
  private listen(): void {
    this.micButtons("listening");
    this.v.micState = "listening";
    this.onAir(true, "LISTENING");
    this.nowSpeaking("You", true);
    // No bubble yet: the words show in the composer while they are said, and
    // the thread gets one bubble at send.
    this.liveMsg = null;
    if (!this.speech) this.startSpeech();
    this.armed = true;
    this.mic.arm();
    this.emit();
  }

  async askCancel(): Promise<void> {
    this.cancelled = true;
    this.hush();
    this.stopSpeech();
    this.mic.stop();
    this.removeMsg(this.liveMsg);
    this.liveMsg = null;
    this.micButtons("idle");
    this.v.micState = "";
    this.v.turnBusy = false;
    this.emit();
    await this.resumeNarration();
  }

  /** A turn that produced nothing takes its bubbles back. */
  private dropEmptyTurn(): void {
    this.removeMsg(this.liveMsg);
    this.liveMsg = null;
  }

  async askSend(): Promise<void> {
    // Entered from the detector and the button alike: a second send would
    // upload the same audio and pay for two answers.
    if (this.sending || !this.v.turnBusy || !this.mic.open) return;
    this.sending = true;
    this.stopSpeech();
    if (this.extendTimer) clearTimeout(this.extendTimer);
    this.extendTimer = null;
    const questionFrom = this.mic.captureFrom;
    const samples = this.mic.take();
    const rate = this.mic.rate;
    // Stop ending turns, keep listening for one starting.
    this.armed = false;
    this.mic.disarm();
    this.mic.reset();
    this.micButtons("working");
    const secs = samples.length / rate;
    if (secs < 0.4) {
      this.mic.stop();
      this.dropEmptyTurn();
      this.appendMsg({ who: "Studio", cls: "err", text: say.tooShort(secs) });
      this.micButtons("idle");
      this.v.micState = "";
      this.v.turnBusy = false;
      this.sending = false;
      this.emit();
      await this.resumeNarration();
      return;
    }
    // One bubble, now, with what the browser heard; the server's `heard`
    // replaces it, being what the answer was grounded in.
    this.liveMsg = this.appendMsg({ who: "You", text: this.heardText || "…", cls: "you" });
    this.v.micState = "thinking…";
    this.onAir(true, "THINKING");
    this.nowSpeaking("", false);
    this.emit();

    const wav = toWav(samples, rate);
    const sink = makeSink();
    const ctrl = new AbortController();
    const voice = { sink, ctrl };
    this.voices.add(voice);
    this.answerMsg = null;
    let barged = false;
    let holdMsg: string | null = null;
    let answerHeard = false;
    // Talking while the studio thinks or answers silences it and gives them
    // the floor. Before any answer was heard they were not finished, so their
    // new speech continues the question.
    this.onBarge = () => {
      barged = true;
      const continuing = !answerHeard;
      this.hush();
      if (continuing) {
        this.mic.captureFrom = questionFrom;
        this.removeMsg(this.liveMsg);
        this.removeMsg(this.answerMsg);
        this.removeMsg(holdMsg);
        this.liveMsg = this.answerMsg = holdMsg = null;
      } else {
        this.mic.captureFrom = Math.max(0, this.mic.speechAt - Math.round((rate * PREFIX_MS) / 1000));
      }
      this.startSpeech();
    };
    let said = "";
    let silent: { input_ms?: number } | null = null;
    let who = "Studio";
    try {
      const r = this.resumeAfter;
      const c = r && r.i >= 0 ? this.v.flat[r.i] : null;
      const q = new URLSearchParams({
        slide: String(c ? c.slide.ordinal : 0),
        line: c ? c.line.line_id : "",
        offset_ms: String(Math.round((this.audio?.currentTime || 0) * 1000)),
      });
      let res: Response;
      try {
        res = await fetch(voiceUrl(this.src.sid, q), {
          method: "POST",
          headers: { "Content-Type": "audio/wav" },
          body: wav,
          signal: ctrl.signal,
        });
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") throw e;
        throw new Error(UNREACHABLE, { cause: e });
      }
      if (!res.ok) throw new Error(await refusal(res));
      const reader = res.body?.getReader();
      if (!reader) throw new Error("The answer could not be read. Try again.");
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const got = sseFrames(buf);
        buf = got.rest;
        for (const { ev, data } of got.frames) {
          const text = () => String((JSON.parse(data) as { t?: unknown }).t ?? "");
          if (ev === "hold") {
            // The narrator filling the wait while the answer is prepared.
            if (barged) continue;
            if (!holdMsg) holdMsg = this.appendMsg({ who, text: "", cls: "on" });
            this.setText(holdMsg, text(), false);
          } else if (ev === "hold_audio") {
            if (barged) continue;
            sink.push(b64ToPcm16(data));
          } else if (ev === "audio") {
            if (barged) continue;
            answerHeard = true;
            if (!this.answerMsg) {
              this.v.micState = `${who} is answering`;
              this.onAir(true, "ANSWERING");
              this.nowSpeaking(who, true);
              this.emit();
            }
            sink.push(b64ToPcm16(data));
          } else if (ev === "speaker") {
            who = text() || "Studio";
          } else if (ev === "said") {
            this.settle(holdMsg);
            holdMsg = null;
            said += text();
            if (!this.answerMsg) this.answerMsg = this.appendMsg({ who, text: "", cls: "on" });
            this.setText(this.answerMsg, said, true);
          } else if (ev === "heard") {
            if (this.liveMsg) this.setText(this.liveMsg, text(), false);
          } else if (ev === "silent") {
            silent = JSON.parse(data || "{}") as { input_ms?: number };
          } else if (ev === "heard_failed") {
            const id = this.liveMsg;
            if (id) this.patchMsg(id, (m) => ({ ...m, title: text() }));
          } else if (ev === "failed") {
            throw new Error(text());
          }
        }
      }
      if (silent) {
        this.dropEmptyTurn();
        this.appendMsg({ who: "Studio", cls: "err", text: say.silent(silent.input_ms || 0) });
        this.v.micState = "";
      } else {
        if (this.answerMsg) this.setText(this.answerMsg, said, false);
        this.v.micState = "";
        this.emit();
        await new Promise<void>((done) => {
          const t = setTimeout(done, Math.ceil(sink.endsIn() * 1000) + 150);
          ctrl.signal.addEventListener("abort", () => {
            clearTimeout(t);
            done();
          });
        });
      }
    } catch (e) {
      // Cut in on is not a failure: the bubble keeps what was said before.
      if (!barged) {
        if (!this.answerMsg) this.dropEmptyTurn();
        this.appendMsg({ who: "Studio", cls: "err", text: say.noAnswer(errText(e)) });
        this.v.micState = "";
      }
    } finally {
      this.onBarge = null;
      this.voices.delete(voice);
      sink.stop();
      sink.close();
      if (this.answerMsg) {
        if (barged) this.patchMsg(this.answerMsg, (m) => ({ ...m, text: said.trim() + " —", live: false }));
        this.settle(this.answerMsg);
      }
      this.settle(holdMsg);
      this.liveMsg = null;
      this.answerMsg = null;
      this.sending = false;
      this.emit();
    }
    if (barged && !this.cancelled && this.mic.open) {
      // Same turn, their floor again; the narration stays paused.
      this.listen();
      return;
    }
    this.stopSpeech();
    this.mic.stop();
    this.armed = false;
    this.micButtons("idle");
    this.v.turnBusy = false;
    this.emit();
    if (!this.cancelled) await this.resumeNarration();
  }

  /** The interrupted line replays from its start: no millisecond here maps
   * to a word boundary, and a seek lands mid-syllable. After the end there is
   * nothing to go back to. */
  private async resumeNarration(): Promise<void> {
    const r = this.resumeAfter;
    this.resumeAfter = null;
    if (!r) return;
    if (r.ended) {
      this.onAir(false, "ENDED");
      this.nowSpeaking("", false);
      this.emit();
      return;
    }
    await this.startAt(r.i, 0);
  }
}
