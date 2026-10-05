import { describe, expect, it } from "vitest";
import { EMPTY, click, keep, start, toggleAll } from "./pick";

const order = ["a", "b", "c", "d", "e"];

// Ported from the old app's pick tests.
describe("picks", () => {
  it("starts selecting on a tick and stops on the last untick", () => {
    let p = click(EMPTY, "b", false, order);
    expect(p.on && p.set.includes("b")).toBe(true);
    p = click(p, "b", false, order);
    expect(p.on).toBe(false);
  });
  it("keeps selecting from the Select button with nothing ticked", () => {
    let p = start(EMPTY);
    p = click(click(p, "a", false, order), "a", false, order);
    expect(p.on && p.set.length === 0).toBe(true);
  });
  it("ticks a shift range in page order either way", () => {
    const p = click(click(EMPTY, "d", false, order), "b", true, order);
    expect([...p.set].sort()).toEqual(["b", "c", "d"]);
  });
  it("treats shift with nothing before it as a plain tick", () => {
    expect(click(EMPTY, "c", true, order).set).toEqual(["c"]);
  });
  it("selects all, then clears while still selecting", () => {
    let p = toggleAll(click(EMPTY, "a", false, order), order);
    expect(p.set.length).toBe(5);
    p = toggleAll(p, order);
    expect(p.on && p.set.length === 0).toBe(true);
  });
  it("drops what leaves the screen", () => {
    let p = click(click(EMPTY, "a", false, order), "b", false, order);
    p = keep(p, ["b"]);
    expect(p.set).toEqual(["b"]);
    expect(keep(p, []).on).toBe(false);
  });
});
