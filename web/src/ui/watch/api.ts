// What the watch page asks the server for: the video's script, the video
// itself, and the tutor's answers. A shared video is reached through its
// share, and has no tutor.

import type * as Rest from "@/client/types.gen";
import { apiBase, call, enc } from "../api";
import type { VideoStyle } from "../video";

export type Script = Rest.VideoScript;
export type ScriptLine = Rest.ScriptLine;

/** What the tutor is asked: a question of one's own, or a quick prompt. */
export type Mode = NonNullable<Rest.ExplainReq["mode"]>;

function base(sid: string, share: string | null): string {
  return share ? `/shares/${enc(share)}/sessions/${enc(sid)}` : `/sessions/${enc(sid)}`;
}

export function scriptOf(sid: string, style: VideoStyle, share: string | null): Promise<Script> {
  return call<Script>("GET", `${base(sid, share)}/video/script?style=${style}`);
}

/** The video's file: to play, or with `download` to save. */
export function mediaUrl(sid: string, style: VideoStyle, share: string | null, download = false): string {
  return `${apiBase()}${base(sid, share)}/video?style=${style}${download ? "&download=true" : ""}`;
}

export function explainAt(sid: string, body: Rest.ExplainReq): Promise<Rest.ExplainOut> {
  return call<Rest.ExplainOut>("POST", `/sessions/${enc(sid)}/video/explain`, body);
}
