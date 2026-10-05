import { describe, expect, it } from "vitest";
import { keepSame, sameOr } from "./helpers";

describe("a list read again", () => {
  const key = (v: { id: string }) => v.id;

  it("keeps the rows that did not change as the same objects", () => {
    const a = { id: "a", n: 1 };
    const b = { id: "b", n: 2 };
    const next = keepSame([a, b], [{ id: "a", n: 1 }, { id: "b", n: 3 }], key);
    expect(next[0]).toBe(a);
    expect(next[1]).not.toBe(b);
    expect(next[1]).toEqual({ id: "b", n: 3 });
  });

  it("answers the old list itself when nothing changed", () => {
    const prev = [{ id: "a", n: 1 }];
    expect(keepSame(prev, [{ id: "a", n: 1 }], key)).toBe(prev);
  });

  it("sees a row gone, a row added and a new order", () => {
    const a = { id: "a", n: 1 };
    const b = { id: "b", n: 2 };
    expect(keepSame([a, b], [{ id: "a", n: 1 }], key)).toEqual([a]);
    expect(keepSame([a], [{ id: "c", n: 0 }, { id: "a", n: 1 }], key)).toEqual([{ id: "c", n: 0 }, a]);
    const swapped = keepSame([a, b], [{ id: "b", n: 2 }, { id: "a", n: 1 }], key);
    expect(swapped).toEqual([b, a]);
    expect(swapped[0]).toBe(b);
  });

  it("keeps a value that says the same", () => {
    const v = { t: "x" };
    expect(sameOr(v, { t: "x" })).toBe(v);
    expect(sameOr(v, { t: "y" })).toEqual({ t: "y" });
  });
});

describe("the server's JSON, read", () => {
  it("reads times, strings and numbers the same way everywhere", async () => {
    const { fromWireKind, ms, num, str, toWireKind } = await import("./helpers");
    expect(ms("2026-10-05T00:00:00Z")).toBe(Date.UTC(2026, 9, 5));
    expect(ms(null)).toBe(0);
    expect(str(3)).toBe("");
    expect(str("a")).toBe("a");
    expect(num("0.25")).toBe(0.25);
    expect(num("x")).toBe(0);
    expect(num(null)).toBe(0);
    expect(fromWireKind("slides")).toBe("session");
    expect(fromWireKind("mindmap")).toBe("mindmap");
    expect(toWireKind("session")).toBe("slides");
    expect(toWireKind("audio")).toBe("audio");
  });
});
