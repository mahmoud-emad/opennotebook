import { describe, expect, it } from "vitest";
import { DEFAULT_GATE, FRAME_MS, Gate, HANG_MS, PeakGate, Resampler } from "./playerGate";

// Ported from opennotebook_vad's frame-level tests: the same decisions on a
// probability track, at Silero's 32 ms frames.
const run = (g: Gate, p: number, frames: number) => {
  let ev = 0;
  for (let i = 0; i < frames; i++) ev = g.step(p);
  return ev;
};
const framesFor = (ms: number) => Math.ceil(ms / FRAME_MS);

describe("the gate", () => {
  it("holds no speech in silence", () => {
    const g = new Gate();
    expect(run(g, 0, framesFor(5000))).toBe(0);
    expect(g.heardSpeech).toBe(false);
    expect(g.speechMs).toBe(0);
  });

  it("does not take a click for speech", () => {
    const g = new Gate();
    // One loud frame, then quiet: under the onset, never speech.
    g.step(1);
    expect(run(g, 0, framesFor(3000))).toBe(0);
    expect(g.heardSpeech).toBe(false);
  });

  it("ends the turn after the hang once there was speech", () => {
    const g = new Gate();
    expect(run(g, 0.95, framesFor(1000))).toBe(1);
    expect(run(g, 0, framesFor(DEFAULT_GATE.hangMs) - 2)).toBe(1);
    expect(run(g, 0, 4)).toBe(2);
    // Ended stays ended until reset.
    expect(g.step(1)).toBe(2);
    g.reset();
    expect(g.step(0)).toBe(0);
  });

  it("still ends the turn in a noisy room", () => {
    // A room parking the probability between decay and attack must not keep
    // the turn open for ever.
    const g = new Gate();
    run(g, 0.95, framesFor(800));
    expect(run(g, 0.55, framesFor(DEFAULT_GATE.hangMs) + 4)).toBe(2);
  });

  it("does not cut off a speaker who keeps talking", () => {
    const g = new Gate();
    for (let i = 0; i < 20; i++) {
      expect(run(g, 0.95, framesFor(600))).toBe(1);
      expect(run(g, 0.1, framesFor(1500))).toBe(1);
    }
  });

  it("does not send a cough and two seconds of quiet", () => {
    const g = new Gate();
    run(g, 0.95, 3); // about 100 ms of speech, under the 300 ms floor
    expect(run(g, 0, framesFor(5000))).toBe(1);
  });
});

describe("the loudness fallback", () => {
  it("is the old peak threshold and hang", () => {
    const p = new PeakGate();
    const loud = new Float32Array(512).fill(0.5);
    const quiet = new Float32Array(512).fill(0.01);
    expect(p.step(quiet)).toBe(0);
    expect(p.step(loud)).toBe(1);
    let ev = 1;
    for (let ms = 0; ms < HANG_MS - 64; ms += 32) ev = p.step(quiet);
    expect(ev).toBe(1);
    expect(p.step(quiet)).toBe(1);
    expect(p.step(quiet)).toBe(2);
  });
});

describe("the resampler", () => {
  it("brings 48 kHz to 16 kHz across uneven chunks", () => {
    const rs = new Resampler(48000);
    const ramp = Float32Array.from({ length: 4800 }, (_, i) => i);
    const out: number[] = [];
    for (let at = 0; at < ramp.length; at += 333) rs.push(ramp.subarray(at, at + 333), out);
    expect(out.length).toBeGreaterThanOrEqual(1599);
    expect(out.length).toBeLessThanOrEqual(1600);
    // Every third sample, with nothing dropped or repeated at the seams.
    out.forEach((v, i) => expect(v).toBeCloseTo(i * 3, 6));
  });

  it("passes 16 kHz through", () => {
    const out: number[] = [];
    new Resampler(16000).push(Float32Array.from([1, 2, 3]), out);
    expect(out).toEqual([1, 2, 3]);
  });

  it("interpolates between seams at an uneven ratio", () => {
    const rs = new Resampler(44100);
    const ramp = Float32Array.from({ length: 4410 }, (_, i) => i);
    const out: number[] = [];
    for (let at = 0; at < ramp.length; at += 128) rs.push(ramp.subarray(at, at + 128), out);
    out.forEach((v, i) => expect(v).toBeCloseTo((i * 44100) / 16000, 3));
  });
});
