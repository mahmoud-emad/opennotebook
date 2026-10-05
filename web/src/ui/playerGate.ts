// When does a question end? The frame-level decision from the old
// `opennotebook_vad` crate (`Gate`), ported as it was, with its thresholds,
// now fed by Silero v5's per-frame speech probability instead of earshot's.
// Silero reads 512 samples at 16 kHz, so a frame is 32 ms here, not 16.
//
// Also here: the loudness gate the page falls back to when the detector does
// not load, and the resampler that brings the microphone to 16 kHz.

/** The detector's rate and frame. */
export const SAMPLE_RATE = 16000;
export const FRAME_SIZE = 512;
export const FRAME_MS = (FRAME_SIZE * 1000) / SAMPLE_RATE;

export type GateConfig = {
  /** Exponential smoothing on the raw probability, in [0, 1); 0 disables it. */
  smoothing: number;
  /** Smoothed probability at or above which a frame starts speech. */
  attack: number;
  /** Smoothed probability below which a frame stops counting as speech. It
   * does not hold the turn open: only an attacking frame restarts the hang. */
  decay: number;
  /** Attacking frames in a row before the turn counts as speaking: what
   * rejects a door slam. */
  onsetFrames: number;
  /** Trailing quiet that ends the turn. */
  hangMs: number;
  /** A clip holding less speech than this did not contain a question. */
  minSpeechMs: number;
};

/** The shipped thresholds, `opennotebook_vad::Config::DEFAULT`. */
export const DEFAULT_GATE: GateConfig = {
  smoothing: 0.4,
  attack: 0.6,
  decay: 0.5,
  onsetFrames: 2,
  hangMs: 2000,
  minSpeechMs: 300,
};

/** 0 nothing worth calling speech yet, 1 speaking (or inside a pause short
 * enough to keep), 2 ended: send the turn. */
export type GateEvent = 0 | 1 | 2;

export class Gate {
  private smoothed = 0;
  private run = 0;
  private speechFrames = 0;
  private quietMs = 0;
  private started = false;
  private ended = false;
  private readonly cfg: GateConfig;
  private readonly frameMs: number;

  constructor(cfg: GateConfig = DEFAULT_GATE, frameMs = FRAME_MS) {
    this.cfg = cfg;
    this.frameMs = frameMs;
  }

  reset(): void {
    this.smoothed = 0;
    this.run = 0;
    this.speechFrames = 0;
    this.quietMs = 0;
    this.started = false;
    this.ended = false;
  }

  get speechMs(): number {
    return this.speechFrames * this.frameMs;
  }

  get heardSpeech(): boolean {
    return this.started;
  }

  /** Feed one frame's raw probability; the state after it. */
  step(raw: number): GateEvent {
    if (this.ended) return 2;
    const c = this.cfg;
    this.smoothed = c.smoothing > 0 ? this.smoothed * c.smoothing + raw * (1 - c.smoothing) : raw;
    if (this.smoothed >= c.attack) {
      this.run += 1;
      if (this.run >= c.onsetFrames) {
        this.started = true;
        this.quietMs = 0;
        this.speechFrames += 1;
      }
    } else {
      this.run = 0;
      if (this.started) {
        // Everything below `attack` counts toward the hang, the band above
        // `decay` included: a room that parks the probability there must not
        // keep the turn open for ever. The band still counts as speech.
        if (this.smoothed >= c.decay) this.speechFrames += 1;
        this.quietMs += this.frameMs;
        if (this.quietMs >= c.hangMs && this.speechMs >= c.minSpeechMs) this.ended = true;
      }
    }
    return this.ended ? 2 : this.started ? 1 : 0;
  }
}

/** Below this peak is not speech: the old page's loudness gate. */
export const SILENCE_PEAK = 0.06;
/** Silence this long after speech ends the turn. */
export const HANG_MS = 2000;

/** The loudness gate, only for when the detector did not load. Any room noise
 * above the threshold keeps the turn open, which is why falling back to it is
 * always said out loud. */
export class PeakGate {
  private heard = false;
  private quietMs = 0;

  reset(): void {
    this.heard = false;
    this.quietMs = 0;
  }

  step(frame: Float32Array, rate = SAMPLE_RATE): GateEvent {
    let peak = 0;
    for (let i = 0; i < frame.length; i++) {
      const q = Math.round(Math.abs(frame[i]!) * 128) / 128;
      if (q > peak) peak = q;
    }
    const ms = (frame.length / rate) * 1000;
    if (peak > SILENCE_PEAK) {
      this.heard = true;
      this.quietMs = 0;
      return 1;
    }
    if (this.heard) {
      this.quietMs += ms;
      return this.quietMs >= HANG_MS ? 2 : 1;
    }
    return 0;
  }
}

/** Any rate to 16 kHz, by linear interpolation, carrying its position across
 * calls: capture buffers are not frame aligned, and treating each on its own
 * drops or repeats a sample at every boundary. */
export class Resampler {
  private readonly step: number;
  private pos = 0;
  private last = 0;
  private primed = false;

  constructor(rateIn: number, rateOut = SAMPLE_RATE) {
    this.step = rateIn / rateOut;
  }

  push(input: Float32Array, out: number[]): void {
    if (this.step === 1) {
      for (let i = 0; i < input.length; i++) out.push(input[i]!);
      return;
    }
    // Positions are relative to `last`, the sample before this input, at -1.
    let p = this.pos;
    for (;;) {
      const i = Math.floor(p);
      if (i + 1 >= input.length) break;
      const a = i < 0 ? (this.primed ? this.last : input[0]!) : input[i]!;
      const b = input[i + 1]!;
      out.push(a + (b - a) * (p - i));
      p += this.step;
    }
    if (input.length) {
      this.last = input[input.length - 1]!;
      this.primed = true;
    }
    this.pos = p - input.length;
  }
}
