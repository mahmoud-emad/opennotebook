// The microphone and the voice that answers: capture in an AudioWorklet, the
// detector that decides when a question has ended, the level meter, the
// browser's live transcript, and the sink that plays an answer as it streams.
// A port of the capture half of the old `player.html`.
//
// The detector is Silero v5 (through @ricky0123/vad-web and onnxruntime-web),
// the same model the server's silence gate runs. It is loaded the first time
// someone asks, never with the page: the ORT wasm is several MB.

import { FRAME_SIZE, Gate, PeakGate, Resampler, SAMPLE_RATE, type GateEvent } from "./playerGate";

/** Capture only. The worklet copies the microphone out in batches and drives
 * nothing: it has no outputs, so the microphone never reaches the speakers
 * that the browser's echo canceller takes its reference from. Kept to plain
 * quotes inside: this is a template literal, and a backtick would end it. */
export const CAPTURE_WORKLET_SRC = `
class OpenNotebookMic extends AudioWorkletProcessor {
  constructor() {
    super();
    // Batched: one message per 128 samples would be hundreds a second on the
    // audio thread's critical path.
    this.out = new Float32Array(2048);
    this.n = 0;
  }
  flush() {
    if (!this.n) return;
    const copy = this.out.slice(0, this.n);
    this.port.postMessage(copy, [copy.buffer]);
    this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch || ch.length === 0) return true;
    if (this.n + ch.length > this.out.length) this.flush();
    this.out.set(ch, this.n);
    this.n += ch.length;
    if (this.n >= this.out.length) this.flush();
    return true;
  }
}
registerProcessor("opennotebook-mic", OpenNotebookMic);
`;

type Model = { process: (frame: Float32Array) => Promise<{ isSpeech: number }>; reset_state: () => void };

let model: Promise<Model> | null = null;

/** Silero, loaded once per page. A failure is not kept, so the next question
 * tries again. */
async function loadModel(): Promise<Model> {
  const [{ Silero }, ort, wasm, onnx] = await Promise.all([
    import("@ricky0123/vad-web/dist/models/silero"),
    import("onnxruntime-web/wasm"),
    import("onnxruntime-web/ort-wasm-simd-threaded.wasm?url"),
    import("@ricky0123/vad-web/dist/silero_vad_v5.onnx?url"),
  ]);
  // One thread: several need cross-origin isolation, which the studio does
  // not ask for, and the model is small.
  ort.env.wasm.numThreads = 1;
  ort.env.wasm.wasmPaths = { wasm: wasm.default };
  const r = await fetch(onnx.default);
  if (!r.ok) throw new Error(`HTTP ${r.status} for the voice detector`);
  const bytes = await r.arrayBuffer();
  return (await Silero.new(ort as never, async () => bytes)) as unknown as Model;
}

function detector(): Promise<Model> {
  if (!model) {
    model = loadModel();
    model.catch(() => {
      model = null;
    });
  }
  return model;
}

/** What the page hears from the microphone. */
export type MicEvents = {
  /** The detector's view changed: 0 idle, 1 speaking, 2 ended. `at` is the
   * capture sample where the frame began. */
  onEvent: (ev: GateEvent, at: number) => void;
  /** An armed detector heard the question end. */
  onEnd: () => void;
  /** The detector did not load; the loudness gate stands in for it. */
  onDegraded: (why: string) => void;
  /** The input's peak, every 60 ms, for the meter. */
  onLevel: (peak: number) => void;
};

/** One microphone, opened once per turn and released in one place. */
export class Mic {
  rate = 48000;
  /** Everything captured this turn, at the device's rate. */
  private chunks: Float32Array[] = [];
  /** How many samples have arrived. */
  captured = 0;
  /** Samples before this are not the question. */
  captureFrom = 0;
  /** Whether the detector ran the model this turn (not the loudness gate). */
  usingModel = false;
  /** The detector's current view: someone is talking. */
  speaking = false;
  /** The capture sample where the current speech began. */
  speechAt = 0;

  private stream: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private meter: ReturnType<typeof setInterval> | null = null;
  private workletUrl: string | null = null;

  private armed = false;
  private ended = false;
  private last: GateEvent = 0;
  private gate = new Gate();
  private peak = new PeakGate();
  private model: Model | null = null;
  private rs: Resampler | null = null;
  private pending: number[] = [];
  /** 16 kHz samples handed to the detector, for each frame's `at`. */
  private fed = 0;
  private queue: Promise<void> = Promise.resolve();
  private gen = 0;
  private readonly ev: MicEvents;

  constructor(ev: MicEvents) {
    this.ev = ev;
  }

  get open(): boolean {
    return this.stream !== null;
  }

  /** Open the microphone. Anything a previous start left open is released
   * first, so a second call costs a wasted prompt and never a leak. */
  async start(): Promise<void> {
    this.teardown();
    const gen = ++this.gen;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    const Ctx =
      window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    const ctx = new Ctx();
    this.ctx = ctx;
    this.rate = ctx.sampleRate;
    const src = ctx.createMediaStreamSource(this.stream);
    this.chunks = [];
    this.captured = 0;
    this.captureFrom = 0;
    this.armed = false;
    this.speaking = false;
    this.speechAt = 0;
    this.resetDetector();
    this.rs = new Resampler(this.rate, SAMPLE_RATE);

    if (!this.workletUrl)
      this.workletUrl = URL.createObjectURL(new Blob([CAPTURE_WORKLET_SRC], { type: "text/javascript" }));
    await ctx.audioWorklet.addModule(this.workletUrl);

    try {
      this.model = await detector();
      this.model.reset_state();
      this.usingModel = true;
    } catch (e) {
      this.model = null;
      this.usingModel = false;
      this.ev.onDegraded(e instanceof Error ? e.message : String(e));
    }
    if (gen !== this.gen) return;

    const node = new AudioWorkletNode(ctx, "opennotebook-mic", {
      numberOfInputs: 1,
      numberOfOutputs: 0,
      channelCount: 1,
    });
    this.node = node;
    node.port.onmessage = (e: MessageEvent<Float32Array>) => this.onPcm(e.data, gen);

    // The meter, and only the meter: an AnalyserNode taps the signal and
    // drives nothing.
    const an = ctx.createAnalyser();
    an.fftSize = 512;
    src.connect(an);
    // No connection to the destination: the worklet is pulled because it has
    // an input.
    src.connect(node);
    const buf = new Uint8Array(an.frequencyBinCount);
    this.meter = setInterval(() => {
      an.getByteTimeDomainData(buf);
      let peak = 0;
      for (const v of buf) peak = Math.max(peak, Math.abs(v - 128) / 128);
      this.ev.onLevel(peak);
    }, 60);
  }

  private onPcm(pcm: Float32Array, gen: number): void {
    if (gen !== this.gen) return;
    this.chunks.push(pcm);
    this.captured += pcm.length;
    this.rs?.push(pcm, this.pending);
    while (this.pending.length >= FRAME_SIZE) {
      const frame = Float32Array.from(this.pending.splice(0, FRAME_SIZE));
      const at = Math.round((this.fed * this.rate) / SAMPLE_RATE);
      this.fed += FRAME_SIZE;
      // In order, one at a time: the model carries state frame to frame.
      this.queue = this.queue.then(() => this.frame(frame, at, gen));
    }
  }

  private async frame(frame: Float32Array, at: number, gen: number): Promise<void> {
    if (gen !== this.gen || this.ended) return;
    let ev: GateEvent;
    if (this.model) {
      let p = 0;
      try {
        p = (await this.model.process(frame)).isSpeech;
      } catch (e) {
        this.model = null;
        this.usingModel = false;
        this.ev.onDegraded(e instanceof Error ? e.message : String(e));
      }
      if (gen !== this.gen || this.ended) return;
      ev = this.model ? this.gate.step(p) : this.peak.step(frame);
    } else {
      ev = this.peak.step(frame);
    }
    if (ev === 2 && !this.armed) {
      // Speech came and went while nobody was listening for a turn: an echo
      // of the studio, or a cough. Forget it and keep watching.
      this.resetDetector();
      ev = 0;
    }
    if (ev === 1 && !this.speaking) this.speechAt = at;
    this.speaking = ev === 1;
    if (ev !== this.last) {
      this.last = ev;
      this.ev.onEvent(ev, at);
    }
    if (ev === 2) {
      this.ended = true;
      this.ev.onEnd();
    }
  }

  private resetDetector(): void {
    this.gate.reset();
    this.peak.reset();
    this.model?.reset_state();
    this.ended = false;
    this.last = 0;
  }

  /** The next end of speech ends the turn. */
  arm(): void {
    this.armed = true;
  }

  /** Keep watching for speech, never end a turn. */
  disarm(): void {
    this.armed = false;
  }

  /** Forget any speech so far: a turn re-opened after a pause that was not
   * the end. */
  reset(): void {
    this.resetDetector();
  }

  /** The question so far, without closing the microphone. */
  take(): Float32Array {
    const n = this.chunks.reduce((a, c) => a + c.length, 0);
    const all = new Float32Array(n);
    let o = 0;
    for (const c of this.chunks) {
      all.set(c, o);
      o += c.length;
    }
    return all.slice(Math.min(this.captureFrom, all.length));
  }

  /** The question, and the microphone closed. */
  stop(): Float32Array {
    const out = this.take();
    this.teardown();
    this.chunks = [];
    return out;
  }

  /** Everything the capture holds, released; safe with nothing open. */
  teardown(): void {
    this.gen++;
    if (this.meter) clearInterval(this.meter);
    this.meter = null;
    this.speaking = false;
    if (this.node) {
      this.node.port.onmessage = null;
      try {
        this.node.disconnect();
      } catch {
        // Already gone.
      }
      this.node = null;
    }
    // Stopping the tracks is what turns the browser's recording light off.
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    if (this.ctx) void this.ctx.close().catch(() => {});
    this.ctx = null;
    this.pending = [];
    this.fed = 0;
    this.queue = Promise.resolve();
  }
}

// ── the browser's live transcript ────────────────────────────────────────────

type Recognition = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onerror: (() => void) | null;
  start: () => void;
  stop: () => void;
};

/** The browser's own recogniser, a convenience that shows words while they
 * are said; the server's `heard` is the record. Null where there is none. */
export function startRecognition(lang: string, onText: (t: string) => void): Recognition | null {
  const w = window as unknown as { SpeechRecognition?: new () => Recognition; webkitSpeechRecognition?: new () => Recognition };
  const R = w.SpeechRecognition || w.webkitSpeechRecognition;
  if (!R) return null;
  const r = new R();
  r.continuous = true;
  r.interimResults = true;
  r.lang = lang;
  r.onresult = (e) => {
    let txt = "";
    for (let i = 0; i < e.results.length; i++) txt += e.results[i]![0]!.transcript;
    onText(txt);
  };
  r.onerror = () => {};
  try {
    r.start();
  } catch {
    return null;
  }
  return r;
}

// ── the answering voice ──────────────────────────────────────────────────────

/** The rate the server speaks answers at. */
export const REPLY_RATE = 24000;

export type Sink = { push: (pcm: Int16Array) => void; endsIn: () => number; stop: () => void; close: () => void };

/** Chunks scheduled back to back on one AudioContext clock: the answer
 * arrives as headerless PCM while it is still being made. */
export function makeSink(): Sink {
  const Ctx =
    window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
  const ctx = new Ctx();
  let cursor = 0;
  let dead = false;
  const live: AudioBufferSourceNode[] = [];
  return {
    push(pcm16) {
      // Stopped means stopped: a frame still in flight after someone talked
      // over the answer must not start it again.
      if (dead) return;
      const f = new Float32Array(pcm16.length);
      for (let i = 0; i < pcm16.length; i++) f[i] = pcm16[i]! / 0x8000;
      const b = ctx.createBuffer(1, f.length, REPLY_RATE);
      b.copyToChannel(f, 0);
      const n = ctx.createBufferSource();
      n.buffer = b;
      n.connect(ctx.destination);
      if (cursor < ctx.currentTime) cursor = ctx.currentTime + 0.02;
      n.start(cursor);
      cursor += b.duration;
      live.push(n);
    },
    endsIn: () => Math.max(0, cursor - ctx.currentTime),
    stop() {
      dead = true;
      cursor = 0;
      live.forEach((n) => {
        try {
          n.stop();
        } catch {
          // Not started, or already over.
        }
      });
    },
    close() {
      void ctx.close().catch(() => {});
    },
  };
}
