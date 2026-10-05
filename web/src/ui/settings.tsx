// The theme, which is this browser's, and the studio's settings as this page
// knows them. A port of the shared half of the old app's `settings.rs`; the
// dialog itself is `SettingsDialog.tsx`.

import { readable } from "./errors";
import { serviceRoot, settingsLoad, storage, type SettingItem, type SettingsDoc } from "./api";
import { store, useStore } from "./store";
import { STYLES } from "./styles";

/** The setting keys the app reads for its hints and defaults. */
export const keys = {
  STYLE: "OPENNOTEBOOK_STYLE",
  SLIDE_COUNT: "OPENNOTEBOOK_SLIDE_COUNT",
  SESSION_MINUTES: "OPENNOTEBOOK_SESSION_MINUTES",
  AUDIO_FORMAT: "OPENNOTEBOOK_AUDIO_FORMAT",
  AUDIO_LENGTH: "OPENNOTEBOOK_AUDIO_LENGTH",
  RESEARCH_DEPTH: "OPENNOTEBOOK_RESEARCH_DEPTH",
  AUTO_NAME: "OPENNOTEBOOK_AUTO_NAME",
  COVERS: "OPENNOTEBOOK_COVERS",
  SPEAKER_COUNT: "OPENNOTEBOOK_SPEAKER_COUNT",
  SPEAKER1_NAME: "OPENNOTEBOOK_SPEAKER1_NAME",
  SPEAKER2_NAME: "OPENNOTEBOOK_SPEAKER2_NAME",
  LANGUAGE: "OPENNOTEBOOK_LANGUAGE",
  CHAT_MODEL: "OPENNOTEBOOK_CHAT_MODEL",
  SHOW_COST: "OPENNOTEBOOK_SHOW_COST",
} as const;

// ── the theme ────────────────────────────────────────────────────────────────

export type ThemePref = "dark" | "light";
const THEME_KEY = "opennotebook.theme";

function loadTheme(): ThemePref {
  try {
    return storage()?.getItem(THEME_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

/** The theme in force, for what the CSS cannot restyle: a cover is drawn by
 * the server in the theme its address asks for. */
export const THEME = store<ThemePref>(loadTheme());

export const themeLabel = (t: ThemePref) => (t === "dark" ? "Dark" : "Light");
export const themeIcon = (t: ThemePref) => (t === "dark" ? "moon-stars" : "sun");

export function applyTheme(t: ThemePref): void {
  THEME.set(t);
  const root = document.documentElement;
  if (t === "light") root.setAttribute("data-bs-theme", "light");
  else root.removeAttribute("data-bs-theme");
  try {
    if (t === "light") storage()?.setItem(THEME_KEY, "light");
    else storage()?.removeItem(THEME_KEY);
  } catch {
    // Storage refused: the theme still applies to this page.
  }
}

// ── the page's copy of the settings ──────────────────────────────────────────

export function effective(i: SettingItem): string {
  return i.value === "" ? i.default : i.value;
}

/** A value as the person reads it: its choice's label, On or Off. */
export function shownValue(i: SettingItem, v: string): string {
  if (i.kind === "toggle") return v === "off" ? "Off" : "On";
  const o = i.options.find((o) => o.value === v);
  if (o) return o.label;
  if (i.model && v) return modelName(v);
  if (i.unit) return `${v} ${i.unit}`;
  return v;
}

/** A model id as a person reads it: `anthropic/claude-haiku-4.5` → "Claude Haiku 4.5". */
export function modelName(id: string): string {
  const name = id.split("/").pop() ?? id;
  return name
    .split("-")
    .map((w) => (/^[A-Za-z]/.test(w) ? w[0]!.toUpperCase() + w.slice(1) : w))
    .join(" ");
}

export type SettingsState = {
  doc: SettingsDoc;
  /** The first read has answered, well or not. */
  loaded: boolean;
  err: string;
  /** The dialog's tab id while it is open. */
  open: string | null;
};

export const SETTINGS = store<SettingsState>({
  doc: { tab_info: [], items: [] },
  loaded: false,
  err: "",
  open: null,
});

/** Read the settings into the page's copy. */
export async function reloadSettings(): Promise<void> {
  try {
    const doc = await settingsLoad();
    SETTINGS.set((s) => ({ ...s, doc, err: "", loaded: true }));
  } catch (e) {
    const err = e instanceof Error ? e.message : readable(String(e));
    SETTINGS.set((s) => ({ ...s, err, loaded: true }));
  }
}

export function openSettings(tab: string | null): void {
  SETTINGS.set((s) => ({ ...s, open: tab }));
}

/** A setting's effective value once the settings are read; undefined before,
 * so a hint never shows a value that may not be the one in force. */
export function settingValue(doc: SettingsDoc, key: string): string | undefined {
  const i = doc.items.find((i) => i.key === key);
  return i ? effective(i) : undefined;
}

export function settingShown(doc: SettingsDoc, key: string): string | undefined {
  const i = doc.items.find((i) => i.key === key);
  return i ? shownValue(i, effective(i)) : undefined;
}

/** The page's settings, with the old app's helpers. */
export function useSettings() {
  const s = useStore(SETTINGS);
  return {
    ...s,
    get: (key: string) => settingValue(s.doc, key),
    shown: (key: string) => settingShown(s.doc, key),
    /** A toggle: on unless it reads `off`, and on while unknown. */
    on: (key: string) => settingValue(s.doc, key) !== "off",
  };
}

/** The Settings tab Settings opens on: the theme, the language, and how
 * collections are named and drawn. */
export const GENERAL = "general";

/** The old Appearance tab's id; a link to it opens General, where the theme is. */
export const APPEARANCE = "appearance";

/** The tab ids the app links to, with their labels for while the settings are
 * still on their way. The server's `tab_info` is the authority. */
export const TAB_LABELS: [string, string][] = [
  [GENERAL, "General"],
  ["defaults", "Generation defaults"],
  ["voices", "Voices"],
  ["conversation", "Live conversation"],
  ["costs", "Costs & limits"],
  ["models", "Models"],
];

/** A Settings tab's glyph, by id. One per tab and no two alike. */
export function tabGlyph(id: string): string {
  return (
    {
      [GENERAL]: "circle-half",
      [APPEARANCE]: "circle-half",
      defaults: "sliders",
      voices: "people",
      language: "translate",
      conversation: "chat-dots",
      costs: "coin",
      models: "cpu",
    }[id] ?? "gear"
  );
}

/** "Settings › Voices": a link that opens Settings on that tab. */
export function SettingsLink({ tab, text }: { tab: string; text?: string }) {
  const s = useStore(SETTINGS);
  const id = tab === APPEARANCE || tab === "language" ? GENERAL : tab;
  const label =
    s.doc.tab_info.find((t) => t.id === id)?.label ??
    TAB_LABELS.find((t) => t[0] === id)?.[1] ??
    "Settings";
  return (
    <button
      className="link-btn set-link"
      title={`Open Settings on ${label}`}
      onClick={() => openSettings(id)}
    >
      {text ?? `Settings › ${label}`}
    </button>
  );
}

/** The thumbnail URL for a style, relative to the bundle's mount. */
export function thumbUrl(id: string): string {
  const known = ["professional", "bento", "instructional", "scientific", "sketchnote", "clay", "bricks"];
  // Under the bundle's mount, so it resolves on every page, /ui/c/<cid> too.
  return `${serviceRoot()}/ui/assets/styles/${known.includes(id) ? id : "editorial"}.jpg`;
}

/** The display name of a style id, falling back to the id itself. */
export function styleLabel(id: string): string {
  return STYLES.find((s) => s.id === id)?.label ?? id;
}
