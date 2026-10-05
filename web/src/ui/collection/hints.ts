// The short lines the Studio says from the settings: who reads a deck, how
// long research takes, which language it writes in. Each is null or quiet
// until the settings are read, so a hint never shows a value that may not be
// the one in force.

import type { SettingsDoc } from "../api";
import { keys, settingValue, type useSettings } from "../settings";
import type { Output } from "../shell";

export type Cfg = ReturnType<typeof useSettings>;

/** The voices a deck from `nSrc` sources is read by, as the settings decide:
 * "Host and Expert", or "Host" alone. */
export function deckVoices(cfg: Cfg, nSrc: number): string | null {
  const host = cfg.get(keys.SPEAKER1_NAME);
  const second = cfg.get(keys.SPEAKER2_NAME);
  const count = cfg.get(keys.SPEAKER_COUNT);
  if (host === undefined || second === undefined || count === undefined) return null;
  const two = count === "1" ? false : count === "2" ? true : nSrc >= 2;
  return two ? `${host} and ${second}` : host;
}

/** "5 slides · about 5 min · Host and Expert": what a deck is made with. */
export function deckSummary(cfg: Cfg, nSrc: number): string | null {
  const slides = cfg.shown(keys.SLIDE_COUNT);
  const minutes = cfg.shown(keys.SESSION_MINUTES);
  const voices = deckVoices(cfg, nSrc);
  if (slides === undefined || minutes === undefined || voices === null) return null;
  return `${slides} · about ${minutes} · ${voices}`;
}

/** The output language when it is not English, the one the voices speak. */
export function otherLanguage(cfg: Cfg): string | null {
  const l = cfg.get(keys.LANGUAGE);
  return l !== undefined && l !== "English" ? l : null;
}

/** Whether both voices speak any output language natively: Microsoft's
 * Multilingual voices do; Kokoro's and the other Microsoft voices keep an
 * English accent. */
export function nativeVoices(cfg: Cfg): boolean {
  const voices = [cfg.get(keys.SPEAKER1_VOICE), cfg.get(keys.SPEAKER2_VOICE)];
  return voices.every((v) => (v ?? "").includes("Multilingual"));
}

/** How long Research a topic reads the web, by the depth setting. */
export function researchTime(cfg: Cfg): string {
  return cfg.get(keys.RESEARCH_DEPTH) === "quick" ? "about a minute" : "a few minutes";
}

/** The settings the server prices each kind by (`sessions_estimate.py`,
 * `mindmaps.py`, `notes.py`): a change in any of these is a different
 * estimate; a change in any other setting is not. The style, format and
 * length are sent with a build's estimate, not read from the settings. */
const ESTIMATE_READS: Record<Output, string[]> = {
  session: [
    keys.SLIDE_COUNT,
    keys.SPEAKER_COUNT,
    keys.SESSION_MINUTES,
    keys.SCRIPT_MODEL,
    keys.SLIDE_MODEL,
    keys.MAX_BUILD_USD,
  ],
  audio: [keys.SPEAKER_COUNT, keys.SESSION_MINUTES, keys.SCRIPT_MODEL, keys.SLIDE_MODEL, keys.MAX_BUILD_USD],
  mindmap: [keys.MINDMAP_MODEL, keys.MAX_BUILD_USD],
  notes: [keys.NOTES_MODEL, keys.MAX_BUILD_USD],
};

/** What an estimate of `kind` reads from the settings, as one string that
 * changes exactly when one of those values does. */
export function estimateSettings(doc: SettingsDoc, kind: Output | null): string {
  if (kind === null) return "";
  return ESTIMATE_READS[kind].map((k) => settingValue(doc, k) ?? "").join("\n");
}
