import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { settingsLoad, type SettingItem, type SettingsDoc } from "./api";
import { SETTINGS, TAB_LABELS, keys, settingShown, tabGlyph } from "./settings";
import { SettingsDialog, grouped, saveLine, tabEl } from "./SettingsDialog";

function item(key: string, group: string, more: Partial<SettingItem> = {}): SettingItem {
  return {
    key,
    tab: "",
    group,
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
    value: "",
    advanced: false,
    ...more,
  };
}

/** A REST setting as the server sends it. */
function rest(more: Record<string, unknown>) {
  return {
    key: "",
    tab: "",
    group: "",
    label: "",
    help: "",
    kind: "text",
    value: "",
    default: "",
    options: [],
    suggestions: [],
    min: null,
    max: null,
    unit: "",
    model: false,
    price: "",
    advanced: false,
    scope: "user",
    ...more,
  };
}

function reply(status: number, body: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// Ported from the old app's settings tests.
describe("settings, the pure parts", () => {
  it("keeps rows in order, a group one block under one heading", () => {
    const g = grouped([item("a", ""), item("b", "Host"), item("c", "Host"), item("d", "Second voice")]);
    expect(g.map(([n, v]) => [n, v.length])).toEqual([
      ["", 1],
      ["Host", 2],
      ["Second voice", 1],
    ]);
  });

  it("gives every tab the app links to its own glyph", () => {
    const glyphs = TAB_LABELS.map((t) => tabGlyph(t[0]));
    expect(glyphs).not.toContain("gear");
    expect(new Set(glyphs).size).toBe(glyphs.length);
  });

  it("reads the server's document into the page's shape", async () => {
    vi.stubGlobal("fetch", () =>
      reply(200, {
        tabs: [{ id: "models", label: "Models", note: "Changing these affects quality and cost.", advanced: true }],
        settings: [
          rest({
            key: keys.CHAT_MODEL,
            tab: "Models",
            group: "Chat & answers",
            label: "Chat & Ask model",
            help: "h",
            default: "google/gemini-2.5-flash-lite",
            options: [
              { value: "google/gemini-2.5-flash-lite", label: "Gemini 2.5 Flash Lite", hint: "$0.1 / $0.4 per M tokens" },
            ],
            suggestions: ["google/gemini-2.5-flash-lite"],
            advanced: true,
            model: true,
            price: "$0.1 / $0.4 per M tokens",
            scope: "instance",
          }),
        ],
      }),
    );
    const doc = await settingsLoad();
    expect(doc.tab_info[0]!.advanced).toBe(true);
    expect(settingShown(doc, keys.CHAT_MODEL)).toBe("Gemini 2.5 Flash Lite");
    expect(doc.items[0]!.model && doc.items[0]!.min === null).toBe(true);
  });

  it("names a model with no label from its id, not raw", () => {
    const doc: SettingsDoc = {
      tab_info: [],
      items: [
        item(keys.CHAT_MODEL, "", {
          tab: "Models",
          value: "amazon/nova-micro-v1",
          default: "google/gemini-2.5-flash-lite",
          options: [{ value: "google/gemini-2.5-flash-lite", label: "Gemini 2.5 Flash Lite", hint: "" }],
          model: true,
        }),
      ],
    };
    expect(settingShown(doc, keys.CHAT_MODEL)).toBe("Nova Micro V1");
  });

  it("words each save state as the old page did", () => {
    expect(saveLine(undefined)).toEqual(["set-st", ""]);
    expect(saveLine({ kind: "saving" })).toEqual(["set-st", "Saving…"]);
    expect(saveLine({ kind: "saved" })).toEqual(["set-st ok", "Saved"]);
    expect(saveLine({ kind: "failed", text: "e" })).toEqual(["set-st err", "e"]);
  });
});

// ── the dialog itself ───────────────────────────────────────────────────────

const DOC_REST = {
  tabs: [
    { id: "general", label: "General", note: "", advanced: false },
    { id: "defaults", label: "Generation defaults", note: "", advanced: false },
    { id: "voices", label: "Voices", note: "", advanced: false },
    { id: "models", label: "Models", note: "Changing these affects quality and cost.", advanced: true },
  ],
  settings: [
    rest({ key: keys.LANGUAGE, tab: "General", label: "Output language", kind: "choice", default: "English", options: [{ value: "English", label: "English", hint: "" }] }),
    rest({ key: keys.SLIDE_COUNT, tab: "Generation defaults", label: "Slides per deck", kind: "number", default: "5", min: 1, max: 20, unit: "slides" }),
    rest({ key: keys.COVERS, tab: "Generation defaults", label: "Covers", kind: "toggle", default: "on" }),
    rest({ key: keys.SPEAKER_COUNT, tab: "Voices", label: "Speakers in a deck", kind: "choice", default: "1", options: [{ value: "1", label: "One", hint: "" }, { value: "2", label: "Two", hint: "" }] }),
    rest({ key: keys.SPEAKER2_NAME, tab: "Voices", group: "Second voice", label: "Second voice name", default: "Ava" }),
  ],
};

let calls: { method: string; url: string; body: string }[] = [];
let patchReply: (key: string, value: string) => Promise<Response>;

beforeEach(() => {
  calls = [];
  SETTINGS.set({ doc: { tab_info: [], items: [] }, loaded: false, err: "", open: "defaults" });
  patchReply = (key, value) => {
    const s = DOC_REST.settings.find((x) => x.key === key)!;
    return reply(200, { ...s, value });
  };
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ method, url, body: String(init?.body ?? "") });
    if (method === "PATCH") {
      const key = decodeURIComponent(url.split("/").pop()!);
      const { value } = JSON.parse(String(init!.body)) as { value: string };
      return patchReply(key, value);
    }
    return reply(200, DOC_REST);
  });
});

async function openDialog(onClose = () => {}) {
  render(<SettingsDialog onClose={onClose} />);
  await act(async () => {});
}

describe("the Settings dialog", () => {
  it("opens on the tab it was asked for, reads the settings again, and takes focus", async () => {
    await openDialog();
    expect(calls.some((c) => c.method === "GET" && c.url.endsWith("/api/settings"))).toBe(true);
    const dialog = screen.getByRole("dialog", { name: "Settings" });
    expect(document.activeElement).toBe(dialog);
    const tab = document.getElementById(tabEl("defaults"))!;
    expect(tab.getAttribute("aria-selected")).toBe("true");
    expect(tab.className).toBe("set-tab on");
    expect(document.getElementById("set-panel")!.getAttribute("aria-labelledby")).toBe("set-tab-defaults");
    expect(screen.getByRole("heading", { level: 3 }).textContent).toBe("Generation defaults");
    // The advanced tabs sit under their own heading, last.
    expect(document.querySelector(".set-tabs-h")!.textContent).toBe("Advanced");
  });

  it("lands on the first server tab when the asked-for tab is not there", async () => {
    SETTINGS.set((s) => ({ ...s, open: "nowhere" }));
    await openDialog();
    expect(screen.getByRole("heading", { level: 3 }).textContent).toBe("General");
  });

  it("moves between tabs with the arrow keys, Home and End", async () => {
    await openDialog();
    const list = screen.getByRole("tablist");
    fireEvent.keyDown(list, { key: "ArrowDown" });
    expect(document.activeElement!.id).toBe(tabEl("voices"));
    fireEvent.keyDown(list, { key: "End" });
    expect(document.activeElement!.id).toBe(tabEl("models"));
    fireEvent.keyDown(list, { key: "ArrowDown" });
    expect(document.activeElement!.id).toBe(tabEl("general"));
    fireEvent.keyDown(list, { key: "ArrowUp" });
    expect(document.activeElement!.id).toBe(tabEl("models"));
    expect(document.querySelector(".set-tnote")!.textContent).toBe("Changing these affects quality and cost.");
  });

  it("closes on Escape, the veil and the close button", async () => {
    const onClose = vi.fn();
    await openDialog(onClose);
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(SETTINGS.get().open).toBeNull();
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(document.querySelector(".set-veil")!);
    fireEvent.click(screen.getByRole("button", { name: "Close settings" }));
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it("opens on General, the first tab, with the theme at its top", async () => {
    SETTINGS.set((s) => ({ ...s, open: null }));
    await openDialog();
    expect(screen.getByRole("heading", { level: 3 }).textContent).toBe("General");
    expect(screen.getByText("Output language")).toBeTruthy();
  });

  it("chooses the theme on General, and an old Appearance link lands there", async () => {
    SETTINGS.set((s) => ({ ...s, open: "appearance" }));
    await openDialog();
    fireEvent.click(screen.getByRole("radio", { name: "Light" }));
    expect(document.documentElement.getAttribute("data-bs-theme")).toBe("light");
    expect(screen.getByRole("radio", { name: "Light" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("heading", { level: 3 }).textContent).toBe("General");
    fireEvent.click(screen.getByRole("radio", { name: "Dark" }));
    expect(document.documentElement.hasAttribute("data-bs-theme")).toBe(false);
  });

  it("saves a change at once and says Saved, then offers Reset", async () => {
    await openDialog();
    const input = screen.getByRole("spinbutton", { name: "Slides per deck, in slides" });
    expect(input.getAttribute("title")).toBe("1 to 20");
    fireEvent.change(input, { target: { value: "8" } });
    expect(screen.getByRole("status").textContent).toBe("Saving…");
    await act(async () => {});
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.url).toMatch(/\/api\/settings\/OPENNOTEBOOK_SLIDE_COUNT$/);
    expect(JSON.parse(patch.body)).toEqual({ value: "8" });
    expect(screen.getByRole("status").textContent).toBe("Saved");
    expect(screen.getByText("Default: 5 slides")).toBeTruthy();
    expect((input as HTMLInputElement).value).toBe("8");
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await act(async () => {});
    expect(JSON.parse(calls.filter((c) => c.method === "PATCH")[1]!.body)).toEqual({ value: "" });
    expect((input as HTMLInputElement).value).toBe("5");
  });

  it("says why a refused value was refused, as an alert", async () => {
    patchReply = () => reply(422, { detail: "Slides per deck must be between 1 and 20." });
    await openDialog();
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "99" } });
    await act(async () => {});
    const alert = screen.getByRole("alert");
    expect(alert.className).toBe("set-st err");
    expect(alert.textContent).toBe("Slides per deck must be between 1 and 20.");
  });

  it("turns a toggle off", async () => {
    await openDialog();
    const sw = screen.getByRole("switch", { name: "Covers" });
    expect(sw.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(sw);
    await act(async () => {});
    expect(JSON.parse(calls.find((c) => c.method === "PATCH")!.body)).toEqual({ value: "off" });
    expect(screen.getByRole("switch", { name: "Covers" }).getAttribute("aria-checked")).toBe("false");
  });

  it("dims the second voice while a deck has one speaker", async () => {
    SETTINGS.set((s) => ({ ...s, open: "voices" }));
    await openDialog();
    const grp = screen.getByRole("heading", { level: 4, name: "Second voice" }).parentElement!;
    expect(grp.className).toBe("set-grp off");
    expect(grp.querySelector(".set-gnote")!.textContent).toBe(
      "Decks use one voice while Speakers in a deck is One. Deep Dive, Critique and Debate still use this one.",
    );
  });

  it("says when the settings could not be loaded, with a way to try again", async () => {
    // An unknown tab falls back to the first one once the read has answered,
    // so the message shows on a tab the page already knows.
    SETTINGS.set((s) => ({ ...s, doc: { tab_info: DOC_REST.tabs, items: [] } }));
    vi.stubGlobal("fetch", () => reply(500, { detail: "The studio could not read its settings." }));
    await openDialog();
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("Settings could not be loaded: The studio could not read its settings.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });
});
