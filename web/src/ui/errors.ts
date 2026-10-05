// Every error a person sees, in words they can act on. A port of the old
// app's `errors.rs`.
//
// The server now words its own failures and sends them as `{"detail": …}`,
// which `api.ts` shows as they are. What remains for this file is what the
// server cannot word (it was never reached) and any text that arrives from
// elsewhere: `readable` says the common failures as one plain sentence and
// tidies anything else. It can be applied twice.

const KNOWN: [string[], string][] = [
  [["no sources"], "Add a source first: a link, a note, or a topic to research."],
  [
    [
      "out of credit",
      "insufficient credit",
      "insufficient_quota",
      "credit balance",
      "more credits",
      "quota exhausted",
      "exceeded your current quota",
      "http 402",
    ],
    "The AI account is out of credit, so nothing can be read or made right now. " +
      "Add credit at openrouter.ai/settings/credits, then try again.",
  ],
  [
    ["refused the key", "invalid api key", "no auth credentials", "missing authentication", "http 401"],
    "The AI provider refused the studio's API key. Check the key, then try again.",
  ],
  [
    ["rate limited", "too many requests", "http 429"],
    "The AI provider is busy right now. Wait a moment and try again.",
  ],
  [
    ["is the model id right", "model not found", "no endpoints found", "not a valid model"],
    "The AI model chosen in Settings is not available. Pick another one in Settings › Models.",
  ],
  [
    ["provider is unavailable", "upstream", "bad gateway", "service unavailable", "gateway timeout"],
    "The AI provider is not answering right now. Try again in a minute.",
  ],
  [
    ["network error", "failed to fetch", "networkerror", "load failed", "connection refused"],
    "The studio cannot be reached. Check your connection and that the studio is running, then try again.",
  ],
];

const MAX_CHARS = 220;

/** The fixed sentence for a failure people meet often, if `raw` is one. */
export function known(raw: string): string | null {
  const low = raw.toLowerCase();
  for (const [keys, say] of KNOWN) if (keys.some((k) => low.includes(k))) return say;
  return null;
}

/** `raw` as a person should read it. */
export function readable(raw: string): string {
  const say = known(raw);
  if (say) return say;
  const m = /^\s*HTTP (\d{3})\s*$/.exec(raw);
  if (m) {
    const code = Number(m[1]);
    if (code === 404) return "That is no longer there. Reload the page and try again.";
    if (code === 408 || code === 504) return "The studio took too long to answer. Try again.";
    if (code >= 500)
      return "The studio ran into a problem. Try again; if it keeps happening, restart the studio.";
    return "The studio refused that request. Reload the page and try again.";
  }
  return tidy(raw);
}

/** Any message made presentable without changing what it says. */
export function tidy(raw: string): string {
  let msg = withoutJson(raw);
  const i = msg.indexOf(" { ");
  if (i >= 0) {
    let head = msg.slice(0, i).trimEnd();
    const sp = head.lastIndexOf(" ");
    if (sp > 0 && /^[A-Z]/.test(head.slice(sp + 1))) head = head.slice(0, sp);
    msg = head.replace(/[: ]+$/, "");
  }
  let s = msg.split(/\s+/).filter(Boolean).join(" ");
  if (!s) return "Something went wrong. Try again.";
  if ([...s].length > MAX_CHARS) {
    let cut = [...s].slice(0, MAX_CHARS).join("");
    const sp = cut.lastIndexOf(" ");
    if (sp > 0) cut = cut.slice(0, sp);
    s = cut.replace(/[,;: ]+$/, "") + "…";
  }
  s = s[0]!.toUpperCase() + s.slice(1);
  if (/[\p{L}\p{N})`]$/u.test(s)) s += ".";
  return s;
}

function withoutJson(msg: string): string {
  const i = msg.indexOf("{");
  if (i < 0) return msg.trim();
  let said: string | null = null;
  try {
    const v = JSON.parse(msg.slice(i)) as { error?: { message?: unknown }; message?: unknown };
    if (typeof v?.error?.message === "string") said = v.error.message;
    else if (typeof v?.message === "string") said = v.message;
  } catch {
    // Not JSON.
  }
  const before = msg.slice(0, i).trim().replace(/:$/, "").trim();
  if (said !== null) return before ? `${before}: ${said}` : said;
  const sp = before.lastIndexOf(" ");
  const name = before.slice(sp + 1);
  if (sp > 0 && /^[A-Z][A-Za-z0-9]*$/.test(name)) return before.slice(0, sp).replace(/[: ]+$/, "");
  return before;
}
