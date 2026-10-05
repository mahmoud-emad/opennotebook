// Where the player reads from: an output of yours, or one a share includes.
// The two differ only in the prefix of every address and in where the
// playhead is kept: yours on the server, a shared one's in this browser, since
// nobody writes to someone else's output.

import type * as Rest from "@/client/types.gen";
import { apiBase, call, enc, storage } from "./api";
import { sessionEventsUrl } from "./api-studio";
import { sessionOf, type SessionDoc } from "./playerModel";

/** Which output, and through which share when it is not yours. */
export type Source = { sid: string; share: string | null };

export type Playhead = {
  slide_ordinal: number;
  line_id: string;
  offset_ms: number;
  state: "idle" | "playing" | "paused" | "finished";
};

export const IDLE: Playhead = { slide_ordinal: 0, line_id: "", offset_ms: 0, state: "idle" };

/** The output's path under /api. */
export function outputPath(src: Source): string {
  return src.share ? `/shares/${enc(src.share)}/sessions/${enc(src.sid)}` : `/sessions/${enc(src.sid)}`;
}

export const lineAudioUrl = (src: Source, lineId: string) =>
  `${apiBase()}${outputPath(src)}/audio/${enc(lineId)}`;
export const slideUrl = (src: Source, ordinal: number) => `${apiBase()}${outputPath(src)}/slides/${ordinal}`;
export const episodeUrl = (src: Source) => `${apiBase()}${outputPath(src)}/episode`;
/** Asking aloud: yours only. */
export const voiceUrl = (sid: string, q: URLSearchParams) => `${apiBase()}/sessions/${enc(sid)}/voice?${q}`;
/** A build's progress, while it prepares: yours only. */
export const eventsUrl = (src: Source) => (src.share ? null : sessionEventsUrl(src.sid));

export async function loadSession(src: Source): Promise<SessionDoc> {
  return sessionOf(await call<Rest.SessionDetail>("GET", outputPath(src)));
}

/** Where playback is kept: the server for yours, this browser for a share's. */
export type Heads = { get: () => Promise<Playhead>; put: (h: Playhead) => Promise<void> };

export function headsFor(src: Source): Heads {
  if (!src.share) {
    const path = `/sessions/${enc(src.sid)}/playback`;
    return {
      get: () => call<Playhead>("GET", path),
      put: async (h) => {
        await call("PUT", path, h);
      },
    };
  }
  const key = `opennotebook.playhead.${src.share}.${src.sid}`;
  return {
    get: async () => {
      try {
        const v = JSON.parse(storage()?.getItem(key) ?? "null") as Playhead | null;
        return v && typeof v.offset_ms === "number" ? v : IDLE;
      } catch {
        return IDLE;
      }
    },
    put: async (h) => {
      try {
        storage()?.setItem(key, JSON.stringify(h));
      } catch {
        // Storage refused: the place is kept on this page only.
      }
    },
  };
}
