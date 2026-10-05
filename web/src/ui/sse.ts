// Server-sent events read off a response body: what the chat's turns and the
// player's spoken answers stream, since `EventSource` cannot POST.

export type Frame = { ev: string | null; data: string };

/** Complete server-sent event frames out of `buf`, and what is left over for
 * the next chunk. Only one space after `data:` is the field's; the payload
 * keeps the rest. */
export function sseFrames(buf: string): { frames: Frame[]; rest: string } {
  const frames: Frame[] = [];
  let cut: number;
  while ((cut = buf.indexOf("\n\n")) >= 0) {
    const frame = buf.slice(0, cut);
    buf = buf.slice(cut + 2);
    let ev: string | null = null;
    const data: string[] = [];
    for (const ln of frame.split("\n")) {
      if (ln.startsWith("event:")) ev = ln.slice(6).trim();
      else if (ln.startsWith("data:")) data.push(ln.slice(5).replace(/^ /, ""));
    }
    frames.push({ ev, data: data.join("\n") });
  }
  return { frames, rest: buf };
}
