import { describe, expect, it } from "vitest";
import { parseRoute, routePath } from "./routes";

const coll = (cid: string, open: unknown = null) => ({ kind: "collection", cid, open });
const DISCOVER = { kind: "discover" };

// Ported from the old app's route_tests.
describe("routes", () => {
  it("gives each screen its address", () => {
    expect(parseRoute("/")).toEqual(DISCOVER);
    expect(parseRoute("")).toEqual(DISCOVER);
    expect(parseRoute("/my-collections")).toEqual({ kind: "mine" });
    expect(parseRoute("/my-collections/")).toEqual({ kind: "mine" });
    expect(parseRoute("/shared/s1790000000000")).toEqual({ kind: "shared", id: "s1790000000000" });
    expect(parseRoute("/c/s1790000000000")).toEqual(coll("s1790000000000"));
    expect(parseRoute("/c/s1/mind-map/m2")).toEqual(coll("s1", { kind: "map", id: "m2" }));
    expect(parseRoute("/c/s1/study-notes/n2/")).toEqual(coll("s1", { kind: "notes", id: "n2" }));
  });
  // The list's address from before Discover took the root lands on My
  // collections, and is then corrected to its new form.
  it("takes the old list address to My collections", () => {
    expect(parseRoute("/collections")).toEqual({ kind: "mine" });
    expect(parseRoute("/collections/")).toEqual({ kind: "mine" });
    expect(routePath({ kind: "mine" })).toBe("/ui/my-collections");
  });
  // A shared collection's address takes one plain id and nothing else.
  it("gives a share its address", () => {
    expect(routePath({ kind: "shared", id: "s1" })).toBe("/ui/shared/s1");
    expect(parseRoute("/shared/")).toEqual(DISCOVER);
    expect(parseRoute("/shared/a b")).toEqual(DISCOVER);
    expect(parseRoute("/shared/s1/extra")).toEqual(DISCOVER);
  });
  // A video's watch page: its style in the query, whiteboard unless slides.
  it("gives a video's watch page its address", () => {
    const v = { kind: "watch", sid: "s1", style: "slides", share: null } as const;
    expect(routePath(v)).toBe("/ui/watch/s1?style=slides");
    expect(parseRoute("/watch/s1", "?style=slides")).toEqual(v);
    expect(parseRoute("/watch/s1", "?style=odd&share=h2")).toEqual({ ...v, style: "whiteboard", share: "h2" });
    expect(parseRoute("/watch/")).toEqual(DISCOVER);
  });
  it("keeps the addresses from before collections", () => {
    expect(parseRoute("/new-session")).toEqual({ kind: "new", output: "session" });
    expect(parseRoute("/new-audio-overview")).toEqual({ kind: "new", output: "audio" });
    expect(parseRoute("/mind-map/s1/m2")).toEqual(coll("s1", { kind: "map", id: "m2" }));
    expect(parseRoute("/study-notes/s1/n2")).toEqual(coll("s1", { kind: "notes", id: "n2" }));
  });
  it("sends nonsense to Discover", () => {
    expect(parseRoute("/c/")).toEqual(DISCOVER);
    expect(parseRoute("/c/a b")).toEqual(DISCOVER);
    expect(parseRoute("/c/s1/elsewhere/x")).toEqual(coll("s1"));
    expect(parseRoute("/whatever")).toEqual(DISCOVER);
  });
});
