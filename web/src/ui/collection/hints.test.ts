import { describe, expect, it } from "vitest";
import type { SettingItem, SettingsDoc } from "../api";
import { keys } from "../settings";
import { estimateSettings } from "./hints";

const item = (key: string, value: string): SettingItem => ({
  key,
  tab: "",
  group: "",
  label: key,
  help: "",
  kind: "text",
  options: [],
  suggestions: [],
  min: null,
  max: null,
  unit: "",
  model: false,
  price: "",
  default: "",
  value,
  advanced: false,
});

const doc = (over: Record<string, string>): SettingsDoc => ({
  tab_info: [],
  items: Object.entries({
    [keys.SLIDE_COUNT]: "5",
    [keys.SCRIPT_MODEL]: "a/one",
    [keys.MINDMAP_MODEL]: "a/map",
    [keys.MAX_BUILD_USD]: "0.50",
    [keys.LANGUAGE]: "English",
    ...over,
  }).map(([k, v]) => item(k, v)),
});

describe("what an estimate reads from the settings", () => {
  it("changes when a setting the estimate is priced by changes", () => {
    expect(estimateSettings(doc({ [keys.SLIDE_COUNT]: "8" }), "session")).not.toBe(estimateSettings(doc({}), "session"));
    expect(estimateSettings(doc({ [keys.MAX_BUILD_USD]: "1" }), "mindmap")).not.toBe(
      estimateSettings(doc({}), "mindmap"),
    );
  });

  it("stays the same when any other setting changes", () => {
    expect(estimateSettings(doc({ [keys.LANGUAGE]: "French" }), "session")).toBe(estimateSettings(doc({}), "session"));
    // A map is not priced by the slides.
    expect(estimateSettings(doc({ [keys.SLIDE_COUNT]: "8" }), "mindmap")).toBe(estimateSettings(doc({}), "mindmap"));
    expect(estimateSettings(doc({}), null)).toBe("");
  });
});
